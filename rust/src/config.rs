//! 配置文件与运行时状态的读写（与 Python 版文件格式保持一致）。

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::time::SystemTime;

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const DEFAULT_LISTEN: &str = "127.0.0.1:17000";

/// 参与选路的上游。
#[derive(Clone, Debug, Serialize)]
pub struct Upstream {
    pub name: String,
    pub addr: String,
    pub enabled: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct Config {
    pub listen: String,
    /// auto = 按顺序取第一个可用的；manual = 固定 active；direct = 全部直连
    pub mode: String,
    pub active: Option<String>,
    pub health_check_interval: i64,
    pub connect_timeout: f64,
    pub failover: bool,
    pub probe_url: String,
    pub probe_timeout: f64,
    /// 监听非本机地址时允许的来源（IP 或网段）；为空表示只接受本机连接。
    pub allow_cidrs: Vec<String>,
    /// 拒绝来源（IP 或网段），优先级高于 allow_cidrs；回环同样不受限。
    pub deny_cidrs: Vec<String>,
    pub upstreams: Vec<Upstream>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            listen: DEFAULT_LISTEN.to_string(),
            mode: "auto".to_string(),
            active: None,
            health_check_interval: 10,
            connect_timeout: 3.0,
            failover: true,
            probe_url: "http://connectivitycheck.gstatic.com/generate_204".to_string(),
            probe_timeout: 3.0,
            allow_cidrs: Vec::new(),
            deny_cidrs: Vec::new(),
            upstreams: Vec::new(),
        }
    }
}

impl Config {
    pub fn is_direct(&self) -> bool {
        self.mode == "direct"
    }

    pub fn is_manual(&self) -> bool {
        self.mode == "manual"
    }

    pub fn interval(&self) -> i64 {
        self.health_check_interval.max(2)
    }

    pub fn connect_timeout(&self) -> std::time::Duration {
        std::time::Duration::from_secs_f64(self.connect_timeout.max(0.001))
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Health {
    #[serde(default)]
    pub tcp: bool,
    #[serde(default)]
    pub http: bool,
    #[serde(default)]
    pub ms: Option<u64>,
}

// --------------------------------------------------------------------------
// 路径
// --------------------------------------------------------------------------

pub fn home() -> PathBuf {
    if let Ok(h) = std::env::var("PSWITCH_HOME") {
        if !h.is_empty() {
            return PathBuf::from(h);
        }
    }
    let base = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(base).join(".config").join("proxy_switch")
}

pub fn config_path() -> PathBuf {
    home().join("config.json")
}

pub fn state_path() -> PathBuf {
    home().join("state.json")
}

pub fn pid_path() -> PathBuf {
    home().join("pswitch.pid")
}

pub fn log_path() -> PathBuf {
    home().join("pswitch.log")
}

pub fn config_hint() -> String {
    format!("配置文件: {}", config_path().display())
}

// --------------------------------------------------------------------------
// JSON 读写
// --------------------------------------------------------------------------

fn read_value(path: &PathBuf) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

pub fn write_json<T: Serialize>(path: &PathBuf, data: &T) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let text = serde_json::to_string_pretty(data).unwrap_or_else(|_| "{}".to_string());
    let tmp = path.with_file_name(format!(
        "{}.tmp",
        path.file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default()
    ));
    fs::write(&tmp, text)?;
    fs::rename(&tmp, path)
}

// --------------------------------------------------------------------------
// 配置
// --------------------------------------------------------------------------

/// 没有配置文件就创建一份（不含任何上游）。返回是否新建。
pub fn ensure_config() -> bool {
    let _ = fs::create_dir_all(home());
    if config_path().exists() {
        return false;
    }
    let _ = write_json(&config_path(), &Config::default());
    true
}

fn num_i64(v: Option<&Value>) -> Option<i64> {
    match v? {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn num_f64(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn truthy(v: &Value, default: bool) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Null => false,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(default),
        Value::String(s) => !s.is_empty(),
        _ => true,
    }
}

/// 宽松解析：缺字段用默认值，坏字段忽略，与 Python 版的容错行为一致。
pub fn load_config() -> Config {
    ensure_config();
    let mut cfg = Config::default();
    let raw = read_value(&config_path());
    let Some(obj) = raw.as_ref().and_then(|v| v.as_object()) else {
        return cfg;
    };

    if let Some(v) = obj.get("listen").and_then(Value::as_str) {
        if !v.is_empty() {
            cfg.listen = v.to_string();
        }
    }
    if let Some(v) = obj.get("mode").and_then(Value::as_str) {
        if !v.is_empty() {
            cfg.mode = v.to_string();
        }
    }
    if let Some(v) = obj.get("active").and_then(Value::as_str) {
        if !v.is_empty() {
            cfg.active = Some(v.to_string());
        }
    }
    if let Some(v) = num_i64(obj.get("health_check_interval")) {
        cfg.health_check_interval = v;
    }
    if let Some(v) = num_f64(obj.get("connect_timeout")) {
        cfg.connect_timeout = v;
    }
    if let Some(v) = obj.get("failover") {
        cfg.failover = truthy(v, true);
    }
    if let Some(v) = obj.get("probe_url").and_then(Value::as_str) {
        if !v.is_empty() {
            cfg.probe_url = v.to_string();
        }
    }
    if let Some(v) = num_f64(obj.get("probe_timeout")) {
        cfg.probe_timeout = v;
    }
    if let Some(Value::Array(list)) = obj.get("allow_cidrs") {
        cfg.allow_cidrs = list
            .iter()
            .filter_map(Value::as_str)
            .map(|s| s.to_string())
            .collect();
    }
    if let Some(Value::Array(list)) = obj.get("deny_cidrs") {
        cfg.deny_cidrs = list
            .iter()
            .filter_map(Value::as_str)
            .map(|s| s.to_string())
            .collect();
    }
    if let Some(Value::Array(list)) = obj.get("upstreams") {
        for item in list {
            let Some(o) = item.as_object() else { continue };
            let name = o.get("name").and_then(Value::as_str).unwrap_or("");
            let addr = o.get("addr").and_then(Value::as_str).unwrap_or("");
            if name.is_empty() || addr.is_empty() {
                continue;
            }
            // 缺少 enabled（旧配置）视为启用；只有显式 false 才算禁用
            let enabled = !matches!(o.get("enabled"), Some(Value::Bool(false)));
            cfg.upstreams.push(Upstream {
                name: name.to_string(),
                addr: addr.to_string(),
                enabled,
            });
        }
    }
    cfg
}

pub fn save_config(cfg: &Config) {
    let _ = write_json(&config_path(), cfg);
}

pub fn find_upstream<'a>(cfg: &'a Config, key: &str) -> Option<&'a Upstream> {
    cfg.upstreams
        .iter()
        .find(|u| u.name == key || u.addr == key)
}

pub fn has_upstreams(cfg: &Config) -> bool {
    !cfg.upstreams.is_empty()
}

/// 参与选路的上游：被 disable 的保留在配置里但完全跳过。
pub fn enabled_upstreams(cfg: &Config) -> Vec<Upstream> {
    cfg.upstreams
        .iter()
        .filter(|u| u.enabled)
        .cloned()
        .collect()
}

pub fn config_mtime() -> Option<SystemTime> {
    fs::metadata(config_path()).ok()?.modified().ok()
}

// --------------------------------------------------------------------------
// 运行时状态
// --------------------------------------------------------------------------

#[derive(Serialize, Clone, Debug, Default)]
pub struct State {
    pub pid: u32,
    pub listen: String,
    pub mode: String,
    pub manual: Option<String>,
    pub active: Option<String>,
    pub active_addr: Option<String>,
    pub health: HashMap<String, Health>,
    pub updated: f64,
}

#[derive(Deserialize, Default, Clone, Debug)]
#[allow(dead_code)] // pid/health 仅用于与 Python 版 state.json 保持同构，暂未读取
pub struct StateView {
    #[serde(default)]
    pub pid: u32,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub active: Option<String>,
    #[serde(default)]
    pub active_addr: Option<String>,
    #[serde(default)]
    pub updated: f64,
    #[serde(default)]
    pub health: HashMap<String, Health>,
}

pub fn write_state(state: &State) {
    let _ = write_json(&state_path(), state);
}

pub fn read_state() -> StateView {
    read_value(&state_path())
        .and_then(|v| serde_json::from_value(v).ok())
        .unwrap_or_default()
}
