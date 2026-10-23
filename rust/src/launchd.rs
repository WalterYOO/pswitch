//! macOS launchd 开机自启支持。

use std::path::PathBuf;
use std::process::{Command, Output};

pub fn label() -> String {
    let user = std::env::var("USER")
        .or_else(|_| std::env::var("LOGNAME"))
        .unwrap_or_else(|_| "user".to_string());
    format!("com.{}.pswitch", user)
}

pub fn plist_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join("Library")
        .join("LaunchAgents")
        .join(format!("{}.plist", label()))
}

pub fn installed() -> bool {
    cfg!(target_os = "macos") && plist_path().exists()
}

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// launchd 里跑的永远是当前这个可执行文件。
pub fn program_args() -> Vec<String> {
    match std::env::current_exe() {
        Ok(p) => vec![p.display().to_string(), "serve".to_string()],
        Err(_) => vec!["pswitch".to_string(), "serve".to_string()],
    }
}

pub fn plist_text() -> String {
    let args: String = program_args()
        .iter()
        .map(|a| format!("\n    <string>{}</string>", xml_escape(a)))
        .collect();
    let log = xml_escape(&crate::config::log_path().display().to_string());
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{}</string>
  <key>ProgramArguments</key>
  <array>{}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{}</string>
  <key>StandardErrorPath</key><string>{}</string>
</dict>
</plist>
"#,
        xml_escape(&label()),
        args,
        log,
        log
    )
}

fn launchctl(args: &[String]) -> std::io::Result<Output> {
    Command::new("launchctl").args(args).output()
}

pub fn bootstrap() -> std::io::Result<Output> {
    let uid = unsafe { libc::getuid() };
    launchctl(&[
        "bootstrap".to_string(),
        format!("gui/{}", uid),
        plist_path().display().to_string(),
    ])
}

pub fn bootout() -> std::io::Result<Output> {
    let uid = unsafe { libc::getuid() };
    launchctl(&[
        "bootout".to_string(),
        format!("gui/{}", uid),
        plist_path().display().to_string(),
    ])
}

pub fn write_plist() -> std::io::Result<()> {
    let path = plist_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, plist_text())
}

pub fn remove_plist() {
    let _ = std::fs::remove_file(plist_path());
}
