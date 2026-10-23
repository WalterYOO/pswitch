//! macOS 系统代理指向（networksetup）。

use std::process::{Command, Output};

/// 当前默认路由对应的网络服务名（例如 "Wi-Fi"）。
pub fn active_network_service() -> Option<String> {
    let out = Command::new("route")
        .args(["-n", "get", "default"])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let iface = text.lines().find_map(|line| {
        line.split_once("interface:")
            .map(|(_, rest)| rest.trim().to_string())
    })?;
    if iface.is_empty() {
        return None;
    }

    let order = Command::new("networksetup")
        .arg("-listnetworkserviceorder")
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&order.stdout);
    let mut service: Option<String> = None;
    for line in text.lines() {
        let trimmed = line.trim();
        if let Some(name) = parse_service_line(trimmed) {
            service = Some(name);
            continue;
        }
        if let Some(current) = &service {
            if let Some(pos) = trimmed.find("Device:") {
                let dev = trimmed[pos + "Device:".len()..]
                    .split(')')
                    .next()
                    .unwrap_or("")
                    .trim();
                if dev == iface {
                    return Some(current.clone());
                }
            }
        }
    }
    None
}

/// 匹配 "(1) Wi-Fi" / "(*1) Wi-Fi" 这类服务行。
fn parse_service_line(line: &str) -> Option<String> {
    let rest = line.strip_prefix('(')?;
    let close = rest.find(')')?;
    let num = &rest[..close];
    let num = num.strip_prefix('*').unwrap_or(num);
    if num.is_empty() || !num.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let name = rest[close + 1..].trim();
    if name.is_empty() {
        None
    } else {
        Some(name.to_string())
    }
}

pub fn networksetup(args: &[String]) -> std::io::Result<Output> {
    Command::new("networksetup").args(args).output()
}

pub fn set_commands(svc: &str, host: &str, port: u16) -> Vec<Vec<String>> {
    let p = port.to_string();
    vec![
        vec![
            "-setwebproxy".to_string(),
            svc.to_string(),
            host.to_string(),
            p.clone(),
        ],
        vec![
            "-setsecurewebproxy".to_string(),
            svc.to_string(),
            host.to_string(),
            p.clone(),
        ],
        vec![
            "-setsocksfirewallproxy".to_string(),
            svc.to_string(),
            host.to_string(),
            p.clone(),
        ],
        vec![
            "-setproxybypassdomains".to_string(),
            svc.to_string(),
            "127.0.0.1".to_string(),
            "localhost".to_string(),
        ],
    ]
}

pub fn off_commands(svc: &str) -> Vec<Vec<String>> {
    vec![
        vec![
            "-setwebproxystate".to_string(),
            svc.to_string(),
            "off".to_string(),
        ],
        vec![
            "-setsecurewebproxystate".to_string(),
            svc.to_string(),
            "off".to_string(),
        ],
        vec![
            "-setsocksfirewallproxystate".to_string(),
            svc.to_string(),
            "off".to_string(),
        ],
    ]
}

/// 读取某一类代理状态，返回 (key, value) 原始字符串。
pub fn get_proxy(flag: &str, svc: &str) -> Option<Vec<(String, String)>> {
    let out = networksetup(&[flag.to_string(), svc.to_string()]).ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    Some(
        text.lines()
            .filter_map(|line| {
                line.split_once(':')
                    .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
            })
            .collect(),
    )
}
