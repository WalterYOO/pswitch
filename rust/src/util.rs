//! 进程 / 端口 / 地址解析等小工具。

use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, TcpStream as StdTcpStream, ToSocketAddrs};
use std::os::raw::{c_int, c_void};
use std::os::unix::io::AsRawFd;
use std::time::Duration;

use tokio::net::TcpStream;

// --------------------------------------------------------------------------
// 进程资源限制
// --------------------------------------------------------------------------

/// 拿不到硬上限时的兜底值，仅用于把硬上限换算成目标值。
const FALLBACK_HARD_FD: u64 = 1 << 16;

fn current_limits() -> Option<libc::rlimit> {
    let mut lim: libc::rlimit = unsafe { std::mem::zeroed() };
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut lim) } != 0 {
        return None;
    }
    Some(lim)
}

fn hard_fd_limit() -> u64 {
    match current_limits() {
        Some(lim) if lim.rlim_max != libc::RLIM_INFINITY => lim.rlim_max as u64,
        _ => maxfiles_per_proc(),
    }
}

/// fd 软上限的目标值：硬上限的一半（硬上限是 unlimited 时用 kern.maxfilesperproc 代替）。
pub fn desired_fd_limit() -> u64 {
    hard_fd_limit() / 2
}

/// 把 fd 软上限抬到硬上限的一半。
///
/// launchd 给守护进程的默认软上限只有 256，浏览器连接稍一多就会撞顶：accept 返回
/// EMFILE，而 macOS 的 getaddrinfo 在 fd 耗尽后会退化成 EAI_NONAME，direct 模式于是
/// 把所有域名请求都回成 502（纯 IP 目标反而正常）。
pub fn raise_fd_limit() {
    let mut lim = match current_limits() {
        Some(l) => l,
        None => return,
    };
    let target = desired_fd_limit() as libc::rlim_t;
    if target > lim.rlim_cur {
        let before = lim.rlim_cur;
        lim.rlim_cur = target;
        unsafe {
            libc::setrlimit(libc::RLIMIT_NOFILE, &lim);
        }
        log_info!("fd 软上限: {} -> {}", before, target);
    }
}

#[cfg(target_os = "macos")]
fn maxfiles_per_proc() -> u64 {
    const NAME: &[u8] = b"kern.maxfilesperproc\0";
    let mut value: c_int = 0;
    let mut size = std::mem::size_of::<c_int>() as libc::size_t;
    let ok = unsafe {
        libc::sysctlbyname(
            NAME.as_ptr() as *const libc::c_char,
            &mut value as *mut c_int as *mut c_void,
            &mut size,
            std::ptr::null_mut(),
            0,
        )
    };
    if ok == 0 && value > 0 {
        value as u64
    } else {
        FALLBACK_HARD_FD
    }
}

#[cfg(not(target_os = "macos"))]
fn maxfiles_per_proc() -> u64 {
    FALLBACK_HARD_FD
}

// --------------------------------------------------------------------------
// 套接字选项
// --------------------------------------------------------------------------

const KEEPALIVE_IDLE: c_int = 30;
const KEEPALIVE_INTERVAL: c_int = 10;
const KEEPALIVE_COUNT: c_int = 3;

unsafe fn setsockopt_int(fd: c_int, level: c_int, name: c_int, value: c_int) {
    libc::setsockopt(
        fd,
        level,
        name,
        &value as *const c_int as *const c_void,
        std::mem::size_of::<c_int>() as libc::socklen_t,
    );
}

/// 关闭 Nagle 并开启 TCP keepalive。
///
/// keepalive 是死隧道唯一的回收手段：对端机器掉线时不会有 FIN/RST，纯字节拷贝的
/// 隧道会一直挂着，每条占两个 fd，攒够就把进程顶到上限。
pub fn tune(stream: &TcpStream) {
    let _ = stream.set_nodelay(true);
    let fd = stream.as_raw_fd();
    unsafe {
        setsockopt_int(fd, libc::SOL_SOCKET, libc::SO_KEEPALIVE, 1);
        setsockopt_int(fd, libc::IPPROTO_TCP, keep_idle_opt(), KEEPALIVE_IDLE);
        setsockopt_int(fd, libc::IPPROTO_TCP, libc::TCP_KEEPINTVL, KEEPALIVE_INTERVAL);
        setsockopt_int(fd, libc::IPPROTO_TCP, libc::TCP_KEEPCNT, KEEPALIVE_COUNT);
    }
}

#[cfg(target_os = "macos")]
fn keep_idle_opt() -> c_int {
    libc::TCP_KEEPALIVE
}

#[cfg(not(target_os = "macos"))]
fn keep_idle_opt() -> c_int {
    libc::TCP_KEEPIDLE
}

pub fn split_hostport(s: &str) -> Result<(String, u16), String> {
    let err = || format!("地址格式应为 host:port，收到 '{}'", s);
    let idx = s.rfind(':').ok_or_else(err)?;
    let (host, port) = (&s[..idx], &s[idx + 1..]);
    if host.is_empty() || port.is_empty() || !port.chars().all(|c| c.is_ascii_digit()) {
        return Err(err());
    }
    let p: u32 = port.parse().map_err(|_| err())?;
    if p > 65535 {
        return Err(err());
    }
    Ok((host.to_string(), p as u16))
}

// --------------------------------------------------------------------------
// 允许来源（IP / 网段）
// --------------------------------------------------------------------------

/// 允许来源的网段（IPv4 / IPv6）。
#[derive(Clone, Copy, Debug)]
pub enum IpNet {
    V4(u32, u8),
    V6(u128, u8),
}

impl IpNet {
    pub fn contains(&self, ip: IpAddr) -> bool {
        match (self, ip) {
            (IpNet::V4(net, prefix), IpAddr::V4(addr)) => {
                let mask = if *prefix == 0 {
                    0
                } else {
                    u32::MAX << (32 - *prefix as u32)
                };
                (u32::from(addr) & mask) == (*net & mask)
            }
            (IpNet::V6(net, prefix), IpAddr::V6(addr)) => {
                let mask = if *prefix == 0 {
                    0
                } else {
                    u128::MAX << (128 - *prefix as u32)
                };
                (u128::from(addr) & mask) == (*net & mask)
            }
            _ => false,
        }
    }
}

impl std::fmt::Display for IpNet {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            IpNet::V4(net, prefix) => write!(f, "{}/{}", Ipv4Addr::from(*net), prefix),
            IpNet::V6(net, prefix) => write!(f, "{}/{}", Ipv6Addr::from(*net), prefix),
        }
    }
}

/// 把 IP 或网段规范成 "ip/prefix"：192.168.3.40 -> 192.168.3.40/32。
pub fn normalize_cidr(entry: &str) -> Result<IpNet, String> {
    let text = entry.trim();
    if text.is_empty() {
        return Err("空的 IP / 网段".to_string());
    }
    let (addr_part, prefix_part) = match text.split_once('/') {
        Some((addr, prefix)) => (addr, Some(prefix)),
        None => (text, None),
    };
    let addr: IpAddr = addr_part.parse().map_err(|_| {
        format!("不是合法的 IP 或网段: {text}（示例: 192.168.3.40 / 192.168.3.0/24）")
    })?;
    let max_prefix = match addr {
        IpAddr::V4(_) => 32u8,
        IpAddr::V6(_) => 128u8,
    };
    let prefix = match prefix_part {
        Some(value) => value
            .parse::<u8>()
            .map_err(|_| format!("网段前缀不合法: {text}"))?,
        None => max_prefix,
    };
    if prefix > max_prefix {
        return Err(format!("网段前缀不合法: {text}"));
    }
    Ok(match addr {
        IpAddr::V4(a) => {
            let mask = if prefix == 0 {
                0
            } else {
                u32::MAX << (32 - prefix as u32)
            };
            IpNet::V4(u32::from(a) & mask, prefix)
        }
        IpAddr::V6(a) => {
            let mask = if prefix == 0 {
                0
            } else {
                u128::MAX << (128 - prefix as u32)
            };
            IpNet::V6(u128::from(a) & mask, prefix)
        }
    })
}

/// 规范化一组条目并去重，保持输入顺序。
pub fn normalize_cidrs(entries: &[String]) -> Result<Vec<String>, String> {
    let mut out: Vec<String> = Vec::new();
    for entry in entries {
        let text = normalize_cidr(entry)?.to_string();
        if !out.contains(&text) {
            out.push(text);
        }
    }
    Ok(out)
}

/// 解析允许来源；非法项忽略（手改配置时容错）。
pub fn parse_allow(entries: &[String]) -> Vec<IpNet> {
    entries
        .iter()
        .filter_map(|entry| normalize_cidr(entry).ok())
        .collect()
}

/// 来源 IP 是否允许：回环始终允许，deny 优先于 allow。
pub fn peer_allowed(allow: &[IpNet], deny: &[IpNet], ip: IpAddr) -> bool {
    if ip.is_loopback() {
        return true;
    }
    if deny.iter().any(|net| net.contains(ip)) {
        return false;
    }
    allow.iter().any(|net| net.contains(ip))
}

/// 监听地址是否只面向本机。
pub fn is_loopback_host(host: &str) -> bool {
    if host.is_empty() || host == "localhost" {
        return true;
    }
    host.parse::<IpAddr>()
        .map(|ip| ip.is_loopback())
        .unwrap_or(false)
}

// --------------------------------------------------------------------------
// PID 文件
// --------------------------------------------------------------------------

pub fn read_pid() -> Option<u32> {
    let text = std::fs::read_to_string(crate::config::pid_path()).ok()?;
    text.trim().parse().ok()
}

pub fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if rc == 0 {
        return true;
    }
    io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

pub fn daemon_running() -> Option<u32> {
    match read_pid() {
        Some(pid) if pid_alive(pid) => Some(pid),
        _ => None,
    }
}

pub fn signal_daemon(sig: libc::c_int) {
    if let Some(pid) = daemon_running() {
        unsafe {
            libc::kill(pid as libc::pid_t, sig);
        }
    }
}

// --------------------------------------------------------------------------
// 端口探测（同步，供 CLI 使用）
// --------------------------------------------------------------------------

pub fn port_open(host: &str, port: u16, timeout: Duration) -> bool {
    let addrs = match (host, port).to_socket_addrs() {
        Ok(a) => a,
        Err(_) => return false,
    };
    for addr in addrs {
        if StdTcpStream::connect_timeout(&addr, timeout).is_ok() {
            return true;
        }
    }
    false
}

// --------------------------------------------------------------------------
// 异步拨号
// --------------------------------------------------------------------------

pub async fn connect_host_port(
    host: &str,
    port: u16,
    timeout: Duration,
) -> io::Result<TcpStream> {
    match tokio::time::timeout(timeout, TcpStream::connect((host, port))).await {
        Ok(Ok(stream)) => {
            // 交互式代理场景下 Nagle 只会增加延迟
            stream.set_nodelay(true)?;
            Ok(stream)
        }
        Ok(Err(e)) => Err(e),
        Err(_) => Err(io::Error::new(io::ErrorKind::TimedOut, "connect timeout")),
    }
}

pub async fn connect_tcp(addr: &str, timeout: Duration) -> io::Result<TcpStream> {
    let (host, port) =
        split_hostport(addr).map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
    connect_host_port(&host, port, timeout).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cidr_normalization() {
        let cases = [
            ("192.168.3.40", "192.168.3.40/32"),
            ("192.168.3.40/24", "192.168.3.0/24"),
            ("10.0.0.0/8", "10.0.0.0/8"),
            ("::1", "::1/128"),
            ("2001:db8::/32", "2001:db8::/32"),
        ];
        for (input, want) in cases {
            assert_eq!(normalize_cidr(input).unwrap().to_string(), want, "{input}");
        }
        for bad in ["", "300.1.1.1", "10.0.0.0/33", "abc"] {
            assert!(normalize_cidr(bad).is_err(), "{bad} 应该被拒绝");
        }
    }

    #[test]
    fn cidr_matching() {
        let net = normalize_cidr("192.168.3.0/24").unwrap();
        assert!(net.contains("192.168.3.77".parse().unwrap()));
        assert!(!net.contains("192.168.4.1".parse().unwrap()));
        let v6 = normalize_cidr("2001:db8::/32").unwrap();
        assert!(v6.contains("2001:db8::5".parse().unwrap()));
        assert!(!v6.contains("2001:db9::5".parse().unwrap()));
    }

    #[test]
    fn loopback_is_always_allowed() {
        let allow = parse_allow(&[]);
        assert!(peer_allowed(&allow, &[], "127.0.0.1".parse().unwrap()));
        assert!(peer_allowed(&allow, &[], "::1".parse().unwrap()));
        assert!(!peer_allowed(&allow, &[], "192.168.3.40".parse().unwrap()));
        let allow = parse_allow(&["192.168.3.0/24".to_string()]);
        assert!(peer_allowed(&allow, &[], "192.168.3.40".parse().unwrap()));
        assert!(!peer_allowed(&allow, &[], "192.168.4.1".parse().unwrap()));
    }

    #[test]
    fn deny_wins_over_allow_but_not_loopback() {
        let allow = parse_allow(&["192.168.3.0/24".to_string()]);
        let deny = parse_allow(&["192.168.3.40/32".to_string()]);
        assert!(!peer_allowed(&allow, &deny, "192.168.3.40".parse().unwrap()));
        assert!(peer_allowed(&allow, &deny, "192.168.3.41".parse().unwrap()));
        let deny_loopback = parse_allow(&["127.0.0.1/32".to_string()]);
        assert!(peer_allowed(&[], &deny_loopback, "127.0.0.1".parse().unwrap()));
    }

    #[test]
    fn loopback_hosts() {
        for host in ["127.0.0.1", "::1", "localhost", ""] {
            assert!(is_loopback_host(host), "{host}");
        }
        for host in ["0.0.0.0", "192.168.3.40", "::"] {
            assert!(!is_loopback_host(host), "{host}");
        }
    }

    #[test]
    fn fd_limit_target_is_usable() {
        // 目标值是硬上限的一半，必须远高于 launchd 默认的 256
        assert!(desired_fd_limit() > 256, "目标值太小: {}", desired_fd_limit());
    }

    #[tokio::test]
    async fn tune_accepts_real_sockets() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let accept = tokio::spawn(async move { listener.accept().await.unwrap().0 });
        let client = TcpStream::connect(addr).await.unwrap();
        let server = accept.await.unwrap();
        tune(&client);
        tune(&server);
    }
}
