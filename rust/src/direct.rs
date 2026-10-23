//! direct 模式：pswitch 自己充当 HTTP / SOCKS5 代理，直接连接目标。
//!
//! 转发模式下 pswitch 只是盲转字节，由上游解释代理协议；direct 模式下没有上游，
//! 所以这里要亲自完成 CONNECT / SOCKS5 握手，之后同样双向 pipe。
//!
//! 支持范围（与 Python 版一致）：
//! - HTTP CONNECT（HTTPS、以及任何隧道协议）
//! - 普通 HTTP 代理请求：改写首个请求行为 origin-form，并强制 Connection: close
//! - SOCKS5 CONNECT，无认证；BIND / UDP ASSOCIATE 明确拒绝
//! - SOCKS4 不支持（记录日志后断开）

use std::net::SocketAddr;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;

use crate::relay;
use crate::util;

/// 客户端发来代理请求头的等待上限；建连沿用 cfg["connect_timeout"]
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_HEAD: usize = 65536;

#[derive(Debug)]
pub struct BadRequest(pub String);

struct Head {
    method: String,
    target: String,
    version: String,
    headers: Vec<(String, String)>,
}

// --------------------------------------------------------------------------
// 入口
// --------------------------------------------------------------------------

pub async fn handle(mut client: TcpStream, connect_timeout: Duration, peer: SocketAddr) {
    let mut first = [0u8; 1];
    let n = match timeout(HANDSHAKE_TIMEOUT, client.read(&mut first)).await {
        Ok(Ok(n)) => n,
        _ => return,
    };
    if n == 0 {
        return;
    }

    match first[0] {
        0x05 => {
            if let Err(e) = handle_socks5(client, connect_timeout, peer).await {
                log_debug!("{} direct SOCKS5 请求无效: {}", peer, e.0);
            }
        }
        0x04 => log_warn!("{} 使用 SOCKS4，direct 模式不支持", peer),
        _ => handle_http(client, first[0], connect_timeout, peer).await,
    }
}

// --------------------------------------------------------------------------
// HTTP 代理
// --------------------------------------------------------------------------

async fn handle_http(
    mut client: TcpStream,
    first: u8,
    connect_timeout: Duration,
    peer: SocketAddr,
) {
    let buf = match read_head(&mut client, vec![first]).await {
        Ok(b) => b,
        Err(e) => {
            log_debug!("{} direct HTTP 请求无效: {}", peer, e.0);
            send_http_error(&mut client, 400, "Bad Request").await;
            return;
        }
    };
    let end = find_head_end(&buf).unwrap_or(buf.len()) + 4;
    let end = end.min(buf.len());
    let leftover = buf[end..].to_vec();
    let raw_end = end.saturating_sub(4);

    let head = match parse_head(&buf[..raw_end]) {
        Ok(h) => h,
        Err(e) => {
            log_debug!("{} direct HTTP 请求无效: {}", peer, e.0);
            send_http_error(&mut client, 400, "Bad Request").await;
            return;
        }
    };

    if head.method.eq_ignore_ascii_case("CONNECT") {
        let (host, port) = match split_target(&head.target, None) {
            Ok(v) => v,
            Err(e) => {
                log_debug!("{} direct HTTP 请求无效: {}", peer, e.0);
                send_http_error(&mut client, 400, "Bad Request").await;
                return;
            }
        };
        let remote = match util::connect_host_port(&host, port, connect_timeout).await {
            Ok(r) => r,
            Err(e) => {
                log_warn!("direct CONNECT {}:{} 失败: {}", host, port, e);
                send_http_error(&mut client, 502, "Bad Gateway").await;
                return;
            }
        };
        if timeout(
            HANDSHAKE_TIMEOUT,
            client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n"),
        )
        .await
        .is_err()
        {
            return;
        }
        log_debug!("{} -> direct CONNECT {}:{}", peer, host, port);
        relay::relay(client, remote, leftover, Vec::new()).await;
        return;
    }

    let (host, port, request_line) = match origin_form(&head) {
        Ok(v) => v,
        Err(e) => {
            log_debug!("{} direct HTTP 请求无效: {}", peer, e.0);
            send_http_error(&mut client, 400, "Bad Request").await;
            return;
        }
    };

    // 逐跳头不发给源站；普通请求强制短连接，避免同一连接上切换目标。
    // 带 Upgrade 的请求（ws:// 等）保留连接语义，交给两端自行协商。
    let upgrade = header(&head.headers, "connection")
        .map(|v| v.to_ascii_lowercase().contains("upgrade"))
        .unwrap_or(false);
    let mut lines = vec![request_line];
    for (name, value) in &head.headers {
        let lower = name.to_ascii_lowercase();
        if lower == "proxy-connection"
            || lower == "proxy-authorization"
            || lower == "keep-alive"
            || (!upgrade && lower == "connection")
        {
            continue;
        }
        lines.push(format!("{}: {}", name, value));
    }
    if header(&head.headers, "host").is_none() {
        if port == 80 {
            lines.push(format!("Host: {}", host));
        } else {
            lines.push(format!("Host: {}:{}", host, port));
        }
    }
    if !upgrade {
        lines.push("Connection: close".to_string());
    }
    let mut request = (lines.join("\r\n") + "\r\n\r\n").into_bytes();
    request.extend_from_slice(&leftover);

    let mut remote = match util::connect_host_port(&host, port, connect_timeout).await {
        Ok(r) => r,
        Err(e) => {
            log_warn!("direct {} {}:{} 失败: {}", head.method, host, port, e);
            send_http_error(&mut client, 502, "Bad Gateway").await;
            return;
        }
    };
    if remote.write_all(&request).await.is_err() {
        return;
    }
    log_debug!("{} -> direct {} {}:{}", peer, head.method, host, port);
    relay::relay(client, remote, Vec::new(), Vec::new()).await;
}

async fn read_head(client: &mut TcpStream, mut buf: Vec<u8>) -> Result<Vec<u8>, BadRequest> {
    loop {
        if find_head_end(&buf).is_some() {
            return Ok(buf);
        }
        if buf.len() > MAX_HEAD {
            return Err(BadRequest("请求头过长".to_string()));
        }
        let mut chunk = [0u8; 8192];
        match timeout(HANDSHAKE_TIMEOUT, client.read(&mut chunk)).await {
            Ok(Ok(0)) => return Err(BadRequest("请求头未发送完整就断开".to_string())),
            Ok(Ok(n)) => buf.extend_from_slice(&chunk[..n]),
            Ok(Err(e)) => return Err(BadRequest(format!("读取失败: {}", e))),
            Err(_) => return Err(BadRequest("请求头超时".to_string())),
        }
    }
}

fn find_head_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

fn parse_head(raw: &[u8]) -> Result<Head, BadRequest> {
    let text = String::from_utf8_lossy(raw);
    let mut lines = text.split("\r\n");
    let first = lines.next().unwrap_or("");
    let parts: Vec<&str> = first.split(' ').collect();
    if parts.len() != 3 || !parts[2].starts_with("HTTP/") {
        return Err(BadRequest(format!("请求行无法解析: '{}'", first)));
    }
    let mut headers = Vec::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        match line.split_once(':') {
            Some((name, value)) => {
                headers.push((name.trim().to_string(), value.trim().to_string()))
            }
            None => return Err(BadRequest(format!("请求头无法解析: '{}'", line))),
        }
    }
    Ok(Head {
        method: parts[0].to_string(),
        target: parts[1].to_string(),
        version: parts[2].to_string(),
        headers,
    })
}

fn header<'a>(headers: &'a [(String, String)], key: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case(key))
        .map(|(_, value)| value.as_str())
}

/// 把代理请求目标改写为 origin-form，返回 (host, port, 请求行)。
fn origin_form(head: &Head) -> Result<(String, u16, String), BadRequest> {
    let target = head.target.as_str();
    if let Some(rest) = target.strip_prefix("http://") {
        let end = rest
            .find(['/', '?', '#'])
            .unwrap_or(rest.len());
        let authority = &rest[..end];
        if authority.is_empty() {
            return Err(BadRequest(format!("URL 缺少主机名: '{}'", target)));
        }
        let (host, port) = parse_authority(authority)?;
        let tail = rest[end..].split('#').next().unwrap_or("");
        let path = if tail.is_empty() {
            "/".to_string()
        } else if tail.starts_with('/') {
            tail.to_string()
        } else {
            format!("/{}", tail)
        };
        return Ok((
            host,
            port,
            format!("{} {} {}", head.method, path, head.version),
        ));
    }
    if target.starts_with("https://") {
        // 明文连接里没法对 https 目标做 TLS，正常客户端应该用 CONNECT
        return Err(BadRequest("https 请求必须使用 CONNECT".to_string()));
    }
    if target.starts_with('/') || target == "*" {
        let host_header = header(&head.headers, "host")
            .ok_or_else(|| BadRequest("请求缺少 Host 头".to_string()))?;
        let (host, port) = split_target(host_header, Some(80))?;
        return Ok((
            host,
            port,
            format!("{} {} {}", head.method, target, head.version),
        ));
    }
    Err(BadRequest(format!("无法识别的请求目标: '{}'", target)))
}

fn parse_authority(authority: &str) -> Result<(String, u16), BadRequest> {
    let hostport = authority.rsplit('@').next().unwrap_or(authority);
    if hostport.starts_with('[') {
        return split_target(hostport, Some(80));
    }
    if let Some(idx) = hostport.rfind(':') {
        let port = &hostport[idx + 1..];
        if !port.is_empty() && port.chars().all(|c| c.is_ascii_digit()) {
            let host = &hostport[..idx];
            if host.is_empty() {
                return Err(BadRequest(format!("URL 缺少主机名: '{}'", authority)));
            }
            let p: u16 = port
                .parse()
                .map_err(|_| BadRequest(format!("URL 缺少主机名: '{}'", authority)))?;
            return Ok((host.to_string(), p));
        }
    }
    if hostport.is_empty() {
        return Err(BadRequest(format!("URL 缺少主机名: '{}'", authority)));
    }
    Ok((hostport.to_string(), 80))
}

/// 解析 host:port，支持 [IPv6]:port；没有端口时用 default_port。
fn split_target(s: &str, default_port: Option<u16>) -> Result<(String, u16), BadRequest> {
    let s = s.trim();
    if s.is_empty() {
        return Err(BadRequest("空地址".to_string()));
    }
    if let Some(rest) = s.strip_prefix('[') {
        let end = rest
            .find(']')
            .ok_or_else(|| BadRequest(format!("地址格式错误: '{}'", s)))?;
        let host = &rest[..end];
        let tail = &rest[end + 1..];
        if tail.is_empty() {
            return match default_port {
                Some(p) => Ok((host.to_string(), p)),
                None => Err(BadRequest(format!("缺少端口: '{}'", s))),
            };
        }
        if let Some(port) = tail.strip_prefix(':') {
            if let Ok(p) = port.parse::<u16>() {
                return Ok((host.to_string(), p));
            }
        }
        return Err(BadRequest(format!("地址格式错误: '{}'", s)));
    }
    if let Some(idx) = s.rfind(':') {
        let (host, port) = (&s[..idx], &s[idx + 1..]);
        if !host.is_empty() && !port.is_empty() && port.chars().all(|c| c.is_ascii_digit()) {
            if let Ok(p) = port.parse::<u16>() {
                return Ok((host.to_string(), p));
            }
        }
    }
    match default_port {
        Some(p) => Ok((s.to_string(), p)),
        None => Err(BadRequest(format!("缺少端口: '{}'", s))),
    }
}

async fn send_http_error(client: &mut TcpStream, code: u16, reason: &str) {
    let msg = format!(
        "HTTP/1.1 {} {}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
        code, reason
    );
    let _ = timeout(HANDSHAKE_TIMEOUT, client.write_all(msg.as_bytes())).await;
}

// --------------------------------------------------------------------------
// SOCKS5
// --------------------------------------------------------------------------

async fn handle_socks5(
    mut client: TcpStream,
    connect_timeout: Duration,
    peer: SocketAddr,
) -> Result<(), BadRequest> {
    let n = read_exact_vec(&mut client, 1).await?[0] as usize;
    let methods = read_exact_vec(&mut client, n).await?;
    if !methods.contains(&0x00) {
        let _ = client.write_all(&[0x05, 0xff]).await; // 没有可接受的认证方式
        return Ok(());
    }
    let _ = client.write_all(&[0x05, 0x00]).await; // 无认证

    let hdr = read_exact_vec(&mut client, 4).await?;
    if hdr[0] != 5 {
        return Err(BadRequest(format!("SOCKS5 版本错误: {}", hdr[0])));
    }
    let cmd = hdr[1];
    let atyp = hdr[3];

    let host = match atyp {
        0x01 => {
            let b = read_exact_vec(&mut client, 4).await?;
            std::net::Ipv4Addr::new(b[0], b[1], b[2], b[3]).to_string()
        }
        0x03 => {
            let len = read_exact_vec(&mut client, 1).await?[0] as usize;
            let b = read_exact_vec(&mut client, len).await?;
            String::from_utf8(b).map_err(|_| BadRequest("域名不是 ASCII".to_string()))?
        }
        0x04 => {
            let b = read_exact_vec(&mut client, 16).await?;
            let mut octets = [0u8; 16];
            octets.copy_from_slice(&b);
            std::net::Ipv6Addr::from(octets).to_string()
        }
        _ => {
            let _ = client.write_all(&socks5_reply(0x08, None)).await; // address type not supported
            return Ok(());
        }
    };
    let pb = read_exact_vec(&mut client, 2).await?;
    let port = u16::from_be_bytes([pb[0], pb[1]]);

    if cmd != 0x01 {
        // 只支持 CONNECT
        let _ = client.write_all(&socks5_reply(0x07, None)).await; // command not supported
        return Ok(());
    }

    match util::connect_host_port(&host, port, connect_timeout).await {
        Ok(remote) => {
            let local = remote.local_addr().ok();
            let _ = client.write_all(&socks5_reply(0x00, local)).await;
            log_debug!("{} -> direct SOCKS5 {}:{}", peer, host, port);
            relay::relay(client, remote, Vec::new(), Vec::new()).await;
        }
        Err(e) => {
            log_warn!("direct SOCKS5 {}:{} 失败: {}", host, port, e);
            let _ = client.write_all(&socks5_reply(socks5_error(&e), None)).await;
        }
    }
    Ok(())
}

async fn read_exact_vec(client: &mut TcpStream, n: usize) -> Result<Vec<u8>, BadRequest> {
    let mut buf = vec![0u8; n];
    match timeout(HANDSHAKE_TIMEOUT, client.read_exact(&mut buf)).await {
        Ok(Ok(_)) => Ok(buf),
        Ok(Err(e)) => Err(BadRequest(e.to_string())),
        Err(_) => Err(BadRequest("读取超时".to_string())),
    }
}

fn socks5_reply(rep: u8, sock: Option<SocketAddr>) -> Vec<u8> {
    let mut out = vec![0x05, rep, 0x00];
    match sock {
        Some(SocketAddr::V4(a)) => {
            out.push(0x01);
            out.extend_from_slice(&a.ip().octets());
            out.extend_from_slice(&a.port().to_be_bytes());
        }
        Some(SocketAddr::V6(a)) => {
            out.push(0x04);
            out.extend_from_slice(&a.ip().octets());
            out.extend_from_slice(&a.port().to_be_bytes());
        }
        None => {
            out.push(0x01);
            out.extend_from_slice(&[0, 0, 0, 0]);
            out.extend_from_slice(&[0, 0]);
        }
    }
    out
}

fn socks5_error(e: &std::io::Error) -> u8 {
    match e.kind() {
        std::io::ErrorKind::ConnectionRefused => 0x05, // connection refused
        std::io::ErrorKind::TimedOut => 0x06,          // TTL expired
        _ => {
            let msg = e.to_string().to_ascii_lowercase();
            if msg.contains("lookup") || msg.contains("dns") || msg.contains("resolve") {
                0x04 // host unreachable
            } else {
                0x01 // general failure
            }
        }
    }
}
