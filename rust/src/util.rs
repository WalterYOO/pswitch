//! 进程 / 端口 / 地址解析等小工具。

use std::io;
use std::net::{TcpStream as StdTcpStream, ToSocketAddrs};
use std::time::Duration;

use tokio::net::TcpStream;

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
