//! 上游探活：HTTP 代理请求优先，TCP 连接兜底。

use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;

use crate::config::Health;
use crate::util;

fn dur(secs: f64) -> Duration {
    Duration::from_secs_f64(secs.max(0.001))
}

fn ms_since(t0: Instant) -> u64 {
    t0.elapsed().as_millis() as u64
}

/// TCP 探测，返回 (ok, 毫秒)。
pub async fn probe_tcp(addr: &str, secs: f64) -> (bool, Option<u64>) {
    let t0 = Instant::now();
    let (host, port) = match util::split_hostport(addr) {
        Ok(v) => v,
        Err(_) => return (false, None),
    };
    match timeout(dur(secs), TcpStream::connect((host.as_str(), port))).await {
        Ok(Ok(_stream)) => (true, Some(ms_since(t0))),
        _ => (false, None),
    }
}

/// 以 HTTP 代理方式真实请求一次，验证上游是否真能转发。返回 (ok, 毫秒)。
pub async fn probe_http(addr: &str, url: &str, secs: f64) -> (bool, Option<u64>) {
    let t0 = Instant::now();
    let (host, port) = match util::split_hostport(addr) {
        Ok(v) => v,
        Err(_) => return (false, None),
    };
    let Some((scheme, authority, path)) = split_url(url) else {
        return (false, None);
    };
    let target = format!("{}://{}{}", scheme, authority, path);
    let req = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: pswitch-health\r\nAccept: */*\r\nConnection: close\r\n\r\n",
        target, authority
    );

    let mut stream = match timeout(dur(secs), TcpStream::connect((host.as_str(), port))).await {
        Ok(Ok(s)) => s,
        _ => return (false, None),
    };
    if timeout(dur(secs), stream.write_all(req.as_bytes()))
        .await
        .is_err()
    {
        return (false, None);
    }

    let mut buf: Vec<u8> = Vec::with_capacity(512);
    let mut chunk = [0u8; 4096];
    loop {
        if find_head_end(&buf).is_some() {
            break;
        }
        if buf.len() > 65536 {
            return (false, None);
        }
        match timeout(dur(secs), stream.read(&mut chunk)).await {
            Ok(Ok(0)) => break,
            Ok(Ok(n)) => buf.extend_from_slice(&chunk[..n]),
            _ => return (false, None),
        }
    }
    let ok = status_code(&buf)
        .map(|c| c == 200 || c == 204)
        .unwrap_or(false);
    if ok {
        (true, Some(ms_since(t0)))
    } else {
        (false, None)
    }
}

fn find_head_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

fn status_code(buf: &[u8]) -> Option<u16> {
    let text = String::from_utf8_lossy(buf);
    let first = text.lines().next()?;
    let mut parts = first.split_whitespace();
    let proto = parts.next()?;
    if !proto.starts_with("HTTP/") {
        return None;
    }
    parts.next()?.parse().ok()
}

/// 拆成 (scheme, authority, path)，path 至少是 "/"。
fn split_url(url: &str) -> Option<(&str, &str, String)> {
    let (scheme, rest) = url.split_once("://")?;
    let end = rest
        .find(['/', '?', '#'])
        .unwrap_or(rest.len());
    let authority = &rest[..end];
    if authority.is_empty() {
        return None;
    }
    let tail = &rest[end..];
    let tail = tail.split('#').next().unwrap_or("");
    let path = if tail.is_empty() {
        "/".to_string()
    } else if tail.starts_with('/') {
        tail.to_string()
    } else {
        format!("/{}", tail)
    };
    Some((scheme, authority, path))
}

/// HTTP 探活优先；只通 TCP 的上游标记为 tcp-only，排序时靠后。
pub async fn probe_full(addr: &str, url: &str, secs: f64) -> Health {
    let (ok_http, ms) = probe_http(addr, url, secs).await;
    if ok_http {
        return Health {
            tcp: true,
            http: true,
            ms,
        };
    }
    let (ok_tcp, ms_tcp) = probe_tcp(addr, 1.0).await;
    Health {
        tcp: ok_tcp,
        http: false,
        ms: if ok_tcp { ms_tcp } else { None },
    }
}
