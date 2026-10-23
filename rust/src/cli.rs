//! pswitch 命令行接口。

use std::collections::HashMap;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command as ProcessCommand, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand};

use crate::config::{self, Config, Health, Upstream};
use crate::launchd;
use crate::probe;
use crate::system_proxy;
use crate::util;

#[derive(Parser)]
#[command(name = "pswitch", version, about = "一个本地端口，多个上游代理")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// 前台运行转发进程（调试用，通常由 start / launchd 调用）
    Serve {
        #[arg(long, hide = true)]
        foreground: bool,
        #[arg(long, hide = true)]
        debug: bool,
    },
    /// 启动转发守护进程
    Start,
    /// 停止守护进程
    Stop,
    /// 重启守护进程
    Restart,
    /// 查看状态（含实时可达性）
    Status,
    /// 列出上游
    List,
    /// 打印应设置的代理环境变量
    Env,
    /// 安装为 launchd 开机自启
    Install,
    /// 卸载开机自启
    Uninstall,
    /// 查看版本号
    Version,
    /// 切换出口：use <名字> | use auto | use direct
    Use { target: String },
    /// 添加上游
    Add { name: String, addr: String },
    /// 删除上游
    Remove { name: String },
    /// 查看/调整优先级（auto 按此顺序选）
    Priority {
        /// 要调整的上游，省略则只列出顺序
        name: Option<String>,
        /// 目标位置，1 = 最高优先级
        pos: Option<usize>,
    },
    /// 启用上游（保留配置，重新参与 use / auto 选路）
    Enable { name: String },
    /// 禁用上游（保留配置，不参与 use / auto 选路）
    Disable { name: String },
    /// 经本地端口做一次真实请求
    Test {
        #[arg(long, default_value_t = 8.0)]
        timeout: f64,
    },
    /// 查看最近日志
    Logs {
        #[arg(short = 'n', long, default_value_t = 30)]
        lines: usize,
    },
    /// 管理 macOS 系统代理指向
    System {
        #[command(subcommand)]
        action: SystemAction,
    },
}

#[derive(Subcommand)]
enum SystemAction {
    /// 把系统代理指向本地 pswitch 端口
    On,
    /// 关闭系统代理
    Off,
    /// 查看系统代理状态
    Status,
    /// 直接把系统代理指向某个上游（不经过 pswitch）
    Use {
        /// 上游名字，或 host:port
        target: String,
    },
}

pub fn run() -> u8 {
    let cli = Cli::parse();
    let code = match cli.cmd {
        Cmd::Serve { foreground, debug } => serve(foreground, debug),
        Cmd::Version => {
            // 纯查询，不应该顺手创建配置文件
            println!("pswitch {}", env!("CARGO_PKG_VERSION"));
            0
        }
        other => {
            if config::ensure_config() {
                println!(
                    "已创建配置文件: {}（还没有上游，用 pswitch add 添加）",
                    config::config_path().display()
                );
            }
            dispatch(other)
        }
    };
    code.clamp(0, 255) as u8
}

fn dispatch(cmd: Cmd) -> i32 {
    match cmd {
        Cmd::Serve { .. } | Cmd::Version => 0,
        Cmd::Start => cmd_start(),
        Cmd::Stop => cmd_stop(),
        Cmd::Restart => {
            cmd_stop();
            cmd_start()
        }
        Cmd::Status => cmd_status(),
        Cmd::List => cmd_list(),
        Cmd::Env => cmd_env(),
        Cmd::Install => cmd_install(),
        Cmd::Uninstall => cmd_uninstall(),
        Cmd::Use { target } => cmd_use(&target),
        Cmd::Add { name, addr } => cmd_add(&name, &addr),
        Cmd::Remove { name } => cmd_remove(&name),
        Cmd::Priority { name, pos } => cmd_priority(name.as_deref(), pos),
        Cmd::Enable { name } => set_enabled(&name, true),
        Cmd::Disable { name } => set_enabled(&name, false),
        Cmd::Test { timeout } => cmd_test(timeout),
        Cmd::Logs { lines } => cmd_logs(lines),
        Cmd::System { action } => cmd_system(action),
    }
}

// --------------------------------------------------------------------------
// 小工具
// --------------------------------------------------------------------------

fn block_on<F: std::future::Future>(fut: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("创建 tokio runtime 失败")
        .block_on(fut)
}

fn require_upstreams(cfg: &Config, action: &str) -> bool {
    if config::has_upstreams(cfg) {
        return true;
    }
    println!("还没有配置任何上游，拒绝{}。", action);
    println!("先添加一个，例如:");
    println!("  pswitch add local 127.0.0.1:17890");
    println!("{}", config::config_hint());
    false
}

fn require_enabled_upstreams(cfg: &Config, action: &str) -> bool {
    if !config::enabled_upstreams(cfg).is_empty() {
        return true;
    }
    println!("所有上游都被禁用了，拒绝{}。", action);
    println!("先启用一个，例如: pswitch enable <名字>");
    println!("{}", config::config_hint());
    false
}

fn live_probe(cfg: &Config) -> Vec<Health> {
    let enabled = config::enabled_upstreams(cfg);
    let url = cfg.probe_url.clone();
    let timeout = cfg.probe_timeout;
    block_on(async move {
        let mut handles = Vec::with_capacity(enabled.len());
        for u in &enabled {
            let addr = u.addr.clone();
            let url = url.clone();
            handles.push(tokio::spawn(async move {
                probe::probe_full(&addr, &url, timeout).await
            }));
        }
        let mut out = Vec::with_capacity(handles.len());
        for h in handles {
            out.push(h.await.unwrap_or_default());
        }
        out
    })
}

/// auto 选路：先 HTTP 通，再 TCP 通，最后退回第一个。
fn auto_pick(ups: &[Upstream], probes: &[Health]) -> Option<String> {
    for http in [true, false] {
        for (u, h) in ups.iter().zip(probes.iter()) {
            let ok = if http { h.http } else { h.tcp };
            if ok {
                return Some(u.name.clone());
            }
        }
    }
    ups.first().map(|u| u.name.clone())
}

// --------------------------------------------------------------------------
// 进程管理
// --------------------------------------------------------------------------

fn serve(foreground: bool, debug: bool) -> i32 {
    crate::logging::init(config::log_path(), foreground, debug);
    let rt = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("pswitch")
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("初始化运行时失败: {}", e);
            return 1;
        }
    };
    rt.block_on(crate::proxy::serve_main())
}

fn cmd_start() -> i32 {
    let cfg = config::load_config();
    if !cfg.is_direct() {
        if !require_upstreams(&cfg, "启动") {
            return 1;
        }
        if !require_enabled_upstreams(&cfg, "启动") {
            return 1;
        }
    }
    if let Some(pid) = util::daemon_running() {
        println!("已经在运行 (pid {})", pid);
        return 0;
    }
    let _ = std::fs::create_dir_all(config::home());
    let (host, port) = match util::split_hostport(&cfg.listen) {
        Ok(v) => v,
        Err(e) => {
            println!("{}", e);
            return 1;
        }
    };

    if launchd::installed() {
        match launchd::bootstrap() {
            Ok(out) => {
                let stderr = String::from_utf8_lossy(&out.stderr).to_string();
                if !out.status.success() && !stderr.contains("already bootstrapped") {
                    println!("launchctl 启动失败: {}", stderr.trim());
                    return 1;
                }
            }
            Err(e) => {
                println!("launchctl 启动失败: {}", e);
                return 1;
            }
        }
    } else {
        let file = match std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(config::log_path())
        {
            Ok(f) => f,
            Err(e) => {
                println!("无法打开日志 {}: {}", config::log_path().display(), e);
                return 1;
            }
        };
        let out = match file.try_clone() {
            Ok(f) => f,
            Err(e) => {
                println!("启动失败: {}", e);
                return 1;
            }
        };
        let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("pswitch"));
        let mut child = ProcessCommand::new(exe);
        child
            .arg("serve")
            .stdin(Stdio::null())
            .stdout(Stdio::from(out))
            .stderr(Stdio::from(file));
        unsafe {
            child.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        if let Err(e) = child.spawn() {
            println!("启动失败: {}", e);
            return 1;
        }
    }

    for _ in 0..60 {
        std::thread::sleep(Duration::from_millis(100));
        if let Some(pid) = util::daemon_running() {
            if util::port_open(&host, port, Duration::from_millis(400)) {
                println!("已启动 (pid {})，监听 {}:{}", pid, host, port);
                return 0;
            }
        }
    }
    println!("启动失败或超时，看日志: {}", config::log_path().display());
    1
}

fn cmd_stop() -> i32 {
    if launchd::installed() {
        // 先摘掉 launchd 作业，否则 KeepAlive 会立刻把它拉起来
        let _ = launchd::bootout();
    }
    let Some(pid) = util::daemon_running() else {
        println!("没有在运行");
        return 0;
    };
    for _ in 0..50 {
        if !util::pid_alive(pid) {
            println!("已停止");
            return 0;
        }
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    unsafe {
        libc::kill(pid as libc::pid_t, libc::SIGKILL);
    }
    std::thread::sleep(Duration::from_millis(50));
    if util::pid_alive(pid) {
        println!("已强制停止");
    } else {
        println!("进程已退出");
    }
    0
}

// --------------------------------------------------------------------------
// 状态 / 上游管理
// --------------------------------------------------------------------------

fn cmd_status() -> i32 {
    let cfg = config::load_config();
    let st = config::read_state();
    let pid = util::daemon_running();
    let (host, port) = match util::split_hostport(&cfg.listen) {
        Ok(v) => v,
        Err(e) => {
            println!("{}", e);
            return 1;
        }
    };
    let listening = pid.is_some() && util::port_open(&host, port, Duration::from_millis(400));

    println!("pswitch");
    println!("  配置文件  {}", config::config_path().display());
    match pid {
        Some(pid) => println!("  守护进程  运行中 (pid {})", pid),
        None => println!("  守护进程  未运行  → pswitch start"),
    }
    println!(
        "  监听      {}  {}",
        cfg.listen,
        if listening { "可连接" } else { "不可连接" }
    );

    let direct = cfg.is_direct();

    if !config::has_upstreams(&cfg) {
        if direct {
            println!("  模式      direct 全部直连");
            println!("  当前出口  direct（直接连接目标，不使用上游）");
        }
        println!("  上游      未配置  → pswitch add <名字> <host:port>");
        return 0;
    }

    let enabled = config::enabled_upstreams(&cfg);
    let probes = live_probe(&cfg);
    let health: HashMap<String, Health> = enabled
        .iter()
        .map(|u| u.name.clone())
        .zip(probes.iter().cloned())
        .collect();

    let active: Option<String> = if direct {
        println!("  模式      direct 全部直连");
        println!("  当前出口  direct（直接连接目标，不使用上游）");
        None
    } else {
        let interval = cfg.health_check_interval.max(2);
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let fresh = pid.is_some() && (now - st.updated) < (15.0f64).max(3.0 * interval as f64);
        let state_active_ok = st
            .active
            .as_ref()
            .map(|a| health.contains_key(a))
            .unwrap_or(false);
        let manual_ok = cfg
            .active
            .as_ref()
            .map(|a| health.contains_key(a))
            .unwrap_or(false);

        let active = if fresh && st.mode.as_deref() == Some(cfg.mode.as_str()) && state_active_ok {
            st.active.clone()
        } else if cfg.is_manual() && manual_ok {
            cfg.active.clone()
        } else {
            auto_pick(&enabled, &probes)
        };

        println!(
            "  模式      {}",
            if cfg.is_manual() {
                "manual 固定"
            } else {
                "auto 自动选择"
            }
        );
        if enabled.is_empty() {
            println!("  当前上游  -（所有上游都已禁用）");
        } else {
            println!("  当前上游  {}", active.as_deref().unwrap_or("-"));
            if let Some(manual_target) = cfg.active.as_deref() {
                if cfg.is_manual() && !health.contains_key(manual_target) {
                    println!(
                        "  说明      manual 目标 {} 已禁用或不存在，按 auto 顺序选择",
                        manual_target
                    );
                }
            }
        }
        active
    };

    println!("  上游列表");
    for u in &cfg.upstreams {
        let mark = if active.as_deref() == Some(u.name.as_str()) {
            "*"
        } else {
            " "
        };
        if !u.enabled {
            println!("   {} {:<12} {:<24} 已禁用", mark, u.name, u.addr);
            continue;
        }
        let h = health.get(&u.name).cloned().unwrap_or_default();
        let state = if h.http {
            format!("OK        {:>3}ms", h.ms.unwrap_or(0))
        } else if h.tcp {
            "TCP only".to_string()
        } else {
            "DOWN".to_string()
        };
        println!("   {} {:<12} {:<24} {}", mark, u.name, u.addr, state);
    }
    0
}

fn cmd_use(target: &str) -> i32 {
    let mut cfg = config::load_config();
    if target == "direct" {
        cfg.mode = "direct".to_string();
        config::save_config(&cfg);
        util::signal_daemon(libc::SIGHUP);
        println!("已切换为 direct：所有请求直接连目标，不经过任何上游");
        return 0;
    }
    if !config::has_upstreams(&cfg) {
        println!("还没有配置任何上游。先 pswitch add <名字> <host:port>");
        return 1;
    }
    if target == "auto" {
        cfg.mode = "auto".to_string();
        config::save_config(&cfg);
        util::signal_daemon(libc::SIGHUP);
        println!("已切换为 auto：按顺序自动选择第一个可用的上游");
        return 0;
    }
    let Some(u) = config::find_upstream(&cfg, target).cloned() else {
        let names = config::enabled_upstreams(&cfg)
            .iter()
            .map(|x| x.name.clone())
            .collect::<Vec<_>>()
            .join(", ");
        let names = if names.is_empty() {
            "无（都被禁用了）".to_string()
        } else {
            names
        };
        println!("没有这个上游: {}\n可选: {}, auto, direct", target, names);
        return 1;
    };
    if !u.enabled {
        println!("{} 已被禁用，先 pswitch enable {}", u.name, u.name);
        return 1;
    }
    cfg.mode = "manual".to_string();
    cfg.active = Some(u.name.clone());
    config::save_config(&cfg);
    util::signal_daemon(libc::SIGHUP);
    println!("已切换: {} -> {}", u.name, u.addr);
    0
}

fn cmd_list() -> i32 {
    let cfg = config::load_config();
    if !config::has_upstreams(&cfg) {
        println!("还没有配置任何上游。用 pswitch add <名字> <host:port> 添加。");
        return 0;
    }
    let active = config::read_state().active;
    for (i, u) in cfg.upstreams.iter().enumerate() {
        let mark = if active.as_deref() == Some(u.name.as_str()) {
            "*"
        } else {
            " "
        };
        let tag = if u.enabled { "" } else { "  已禁用" };
        println!(" {} {}. {:<12} {}{}", mark, i + 1, u.name, u.addr, tag);
    }
    0
}

fn cmd_add(name: &str, addr: &str) -> i32 {
    let mut cfg = config::load_config();
    if let Err(e) = util::split_hostport(addr) {
        println!("{}", e);
        return 1;
    }
    if config::find_upstream(&cfg, name).is_some() {
        println!("名字已存在: {}", name);
        return 1;
    }
    cfg.upstreams.push(Upstream {
        name: name.to_string(),
        addr: addr.to_string(),
        enabled: true,
    });
    if cfg.active.as_deref().is_none_or(|s| s.is_empty()) {
        cfg.active = Some(name.to_string());
    }
    config::save_config(&cfg);
    util::signal_daemon(libc::SIGHUP);
    println!("已添加: {} -> {}", name, addr);
    if cfg.upstreams.len() == 1 {
        println!("现在可以启动了: pswitch start");
    }
    0
}

fn cmd_remove(name: &str) -> i32 {
    let mut cfg = config::load_config();
    let before = cfg.upstreams.len();
    cfg.upstreams.retain(|u| u.name != name);
    if cfg.upstreams.len() == before {
        println!("没有这个上游: {}", name);
        return 1;
    }
    if cfg.active.as_deref() == Some(name) {
        cfg.mode = "auto".to_string();
        let enabled = config::enabled_upstreams(&cfg);
        cfg.active = enabled
            .first()
            .map(|u| u.name.clone())
            .or_else(|| cfg.upstreams.first().map(|u| u.name.clone()));
    }
    config::save_config(&cfg);
    util::signal_daemon(libc::SIGHUP);
    println!("已删除: {}", name);
    0
}

fn cmd_priority(name: Option<&str>, pos: Option<usize>) -> i32 {
    let mut cfg = config::load_config();
    if cfg.upstreams.is_empty() {
        println!("还没有配置任何上游。用 pswitch add <名字> <host:port> 添加。");
        return 0;
    }
    let Some(name) = name else {
        println!("优先级顺序（auto 从上到下取第一个可用的，禁用的跳过）：");
        for (i, u) in cfg.upstreams.iter().enumerate() {
            let tag = if u.enabled { "" } else { "  已禁用" };
            println!(" {}. {:<12} {:<24}{}", i + 1, u.name, u.addr, tag);
        }
        return 0;
    };

    let Some(cur) = cfg.upstreams.iter().position(|u| u.name == name) else {
        let names = cfg
            .upstreams
            .iter()
            .map(|x| x.name.clone())
            .collect::<Vec<_>>()
            .join(", ");
        println!("没有这个上游: {}\n可选: {}", name, names);
        return 1;
    };
    let Some(pos) = pos else {
        println!(
            "{} 当前优先级 {}/{}",
            cfg.upstreams[cur].name,
            cur + 1,
            cfg.upstreams.len()
        );
        return 0;
    };
    if pos < 1 || pos > cfg.upstreams.len() {
        println!("位置要在 1..{} 之间", cfg.upstreams.len());
        return 1;
    }
    if pos - 1 == cur {
        println!("{} 已经在第 {} 位", cfg.upstreams[cur].name, pos);
        return 0;
    }
    let u = cfg.upstreams.remove(cur);
    let moved_name = u.name.clone();
    let still_disabled = !u.enabled;
    cfg.upstreams.insert(pos - 1, u);
    config::save_config(&cfg);
    util::signal_daemon(libc::SIGHUP);
    let order = cfg
        .upstreams
        .iter()
        .map(|x| x.name.clone())
        .collect::<Vec<_>>()
        .join(" > ");
    println!("已把 {} 排到第 {} 位: {}", moved_name, pos, order);
    if still_disabled {
        println!(
            "注意：它当前是禁用状态，顺序不参与选路，先 pswitch enable {}",
            moved_name
        );
    }
    0
}

fn set_enabled(name: &str, enabled: bool) -> i32 {
    let mut cfg = config::load_config();
    let Some(idx) = cfg.upstreams.iter().position(|u| u.name == name) else {
        let names = cfg
            .upstreams
            .iter()
            .map(|x| x.name.clone())
            .collect::<Vec<_>>();
        let names = if names.is_empty() {
            "无".to_string()
        } else {
            names.join(", ")
        };
        println!("没有这个上游: {}\n可选: {}", name, names);
        return 1;
    };
    if cfg.upstreams[idx].enabled == enabled {
        println!(
            "{} 已经是{}状态",
            cfg.upstreams[idx].name,
            if enabled { "启用" } else { "禁用" }
        );
        return 0;
    }
    cfg.upstreams[idx].enabled = enabled;
    let addr = cfg.upstreams[idx].addr.clone();
    let u_name = cfg.upstreams[idx].name.clone();
    config::save_config(&cfg);
    util::signal_daemon(libc::SIGHUP);

    if enabled {
        println!("已启用: {} -> {}", u_name, addr);
        return 0;
    }
    println!(
        "已禁用: {} -> {}（配置保留，不参与 use / auto / 故障转移）",
        u_name, addr
    );
    if config::enabled_upstreams(&cfg).is_empty() {
        if cfg.is_direct() {
            println!("警告：所有上游都已禁用；direct 模式不受影响，切回 auto/manual 前需启用一个。");
        } else {
            println!("警告：所有上游都已禁用，转发会全部失败。启用一个: pswitch enable <名字>");
        }
    } else if cfg.is_manual() && cfg.active.as_deref() == Some(u_name.as_str()) {
        println!("注意：它正是当前 manual 固定的上游，守护进程会暂时改用其它可用上游。");
    }
    0
}

// --------------------------------------------------------------------------
// 其他命令
// --------------------------------------------------------------------------

fn cmd_env() -> i32 {
    let cfg = config::load_config();
    let proxy = format!("http://{}", cfg.listen);
    let socks = format!("socks5://{}", cfg.listen);
    for line in [
        format!("export http_proxy=\"{}\"", proxy),
        format!("export https_proxy=\"{}\"", proxy),
        format!("export HTTP_PROXY=\"{}\"", proxy),
        format!("export HTTPS_PROXY=\"{}\"", proxy),
        format!("export all_proxy=\"{}\"", socks),
        format!("export ALL_PROXY=\"{}\"", socks),
        "export no_proxy=\"127.0.0.1,localhost\"".to_string(),
        "export NO_PROXY=\"127.0.0.1,localhost\"".to_string(),
    ] {
        println!("{}", line);
    }
    0
}

fn cmd_test(timeout: f64) -> i32 {
    let cfg = config::load_config();
    if !cfg.is_direct() && !require_upstreams(&cfg, "测试") {
        return 1;
    }
    let proxy_url = format!("http://{}", cfg.listen);
    let proxy = match ureq::Proxy::new(&proxy_url) {
        Ok(p) => p,
        Err(e) => {
            println!("FAIL 代理地址无效: {}", e);
            return 1;
        }
    };
    let agent = ureq::AgentBuilder::new()
        .proxy(proxy)
        .timeout(Duration::from_secs_f64(timeout.max(0.001)))
        .build();

    let mut ok_all = true;
    for url in [
        "http://connectivitycheck.gstatic.com/generate_204",
        "https://www.gstatic.com/generate_204",
    ] {
        let t0 = Instant::now();
        match agent.get(url).call() {
            Ok(resp) => println!(
                "OK   {}  {}  {}ms",
                url,
                resp.status(),
                t0.elapsed().as_millis()
            ),
            Err(e) => {
                ok_all = false;
                println!("FAIL {}  {}", url, e);
            }
        }
    }
    let st = config::read_state();
    if let Some(active) = st.active {
        println!(
            "实际出口: {} ({})",
            active,
            st.active_addr.unwrap_or_default()
        );
    }
    if ok_all {
        0
    } else {
        1
    }
}

fn cmd_logs(lines: usize) -> i32 {
    let path = config::log_path();
    let Ok(text) = std::fs::read_to_string(&path) else {
        println!("暂无日志: {}", path.display());
        return 0;
    };
    let all: Vec<&str> = text.lines().collect();
    let start = all.len().saturating_sub(lines);
    for line in &all[start..] {
        println!("{}", line);
    }
    println!(
        "\n(日志文件: {}，实时跟踪: tail -f {})",
        path.display(),
        path.display()
    );
    0
}

fn cmd_install() -> i32 {
    let cfg = config::load_config();
    if !cfg.is_direct() {
        if !require_upstreams(&cfg, "安装开机自启") {
            return 1;
        }
        if !require_enabled_upstreams(&cfg, "安装开机自启") {
            return 1;
        }
    }
    if !cfg!(target_os = "macos") {
        println!("仅支持 macOS 的 launchd");
        return 1;
    }
    if let Err(e) = launchd::write_plist() {
        println!("写入 plist 失败: {}", e);
        return 1;
    }
    let _ = launchd::bootout();
    match launchd::bootstrap() {
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            if !out.status.success() && !stderr.contains("already bootstrapped") {
                println!("launchctl 失败: {}", stderr.trim());
                return 1;
            }
        }
        Err(e) => {
            println!("launchctl 失败: {}", e);
            return 1;
        }
    }
    let (host, port) = match util::split_hostport(&cfg.listen) {
        Ok(v) => v,
        Err(e) => {
            println!("{}", e);
            return 1;
        }
    };
    for _ in 0..100 {
        if let Some(pid) = util::daemon_running() {
            if util::port_open(&host, port, Duration::from_millis(400)) {
                println!(
                    "已安装开机自启并启动 (pid {})，监听 {}:{}",
                    pid, host, port
                );
                println!("plist: {}", launchd::plist_path().display());
                println!("停止并禁用: pswitch uninstall");
                return 0;
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    println!(
        "已安装开机自启: {}（进程尚未就绪，看日志 {}）",
        launchd::plist_path().display(),
        config::log_path().display()
    );
    println!("停止并禁用: pswitch uninstall");
    0
}

fn cmd_uninstall() -> i32 {
    let _ = launchd::bootout();
    launchd::remove_plist();
    cmd_stop();
    println!("已卸载开机自启");
    0
}

// --------------------------------------------------------------------------
// macOS 系统代理
// --------------------------------------------------------------------------

/// 把 system use 的参数解析成 host:port：上游名字优先，否则按 host:port 直接使用。
fn resolve_system_target(cfg: &Config, target: &str) -> Result<(String, u16), String> {
    if let Some(u) = cfg.upstreams.iter().find(|u| u.name == target) {
        if !u.enabled {
            return Err(format!("{} 已被禁用，先 pswitch enable {}", u.name, u.name));
        }
        return util::split_hostport(&u.addr);
    }
    match util::split_hostport(target) {
        Ok(v) => Ok(v),
        Err(_) => {
            let names = if cfg.upstreams.is_empty() {
                "无".to_string()
            } else {
                cfg.upstreams
                    .iter()
                    .map(|u| u.name.clone())
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            Err(format!(
                "没有这个上游也不是合法地址: {}\n可选: {}，或直接给 host:port",
                target, names
            ))
        }
    }
}

fn cmd_system_use(cfg: &Config, target: &str) -> i32 {
    let (host, port) = match resolve_system_target(cfg, target) {
        Ok(v) => v,
        Err(msg) => {
            println!("{}", msg);
            return 1;
        }
    };
    let Some(svc) = system_proxy::active_network_service() else {
        println!("找不到当前活动的网络服务");
        return 1;
    };
    for args in system_proxy::set_commands(&svc, &host, port) {
        let _ = system_proxy::networksetup(&args);
    }
    if cfg.upstreams.iter().any(|u| u.name == target) {
        println!(
            "系统代理（{}）已直接指向上游 {} ({}:{})",
            svc, target, host, port
        );
    } else {
        println!("系统代理（{}）已直接指向 {}:{}", svc, host, port);
    }
    println!("注意：这不经过 pswitch，use / 故障转移都不生效；恢复本地端口: pswitch system on");
    0
}

fn cmd_system(action: SystemAction) -> i32 {
    let cfg = config::load_config();
    if let SystemAction::Use { target } = &action {
        return cmd_system_use(&cfg, target);
    }
    if !cfg.is_direct() && !require_upstreams(&cfg, "修改系统代理") {
        return 1;
    }
    let is_on = matches!(&action, SystemAction::On);
    if is_on && util::daemon_running().is_none() {
        println!("守护进程未运行，先 pswitch start 或 pswitch install");
        return 1;
    }
    if is_on && !cfg.is_direct() && !require_enabled_upstreams(&cfg, "开启系统代理") {
        return 1;
    }
    let Some(svc) = system_proxy::active_network_service() else {
        println!("找不到当前活动的网络服务");
        return 1;
    };
    let (mut host, port) = match util::split_hostport(&cfg.listen) {
        Ok(v) => v,
        Err(e) => {
            println!("{}", e);
            return 1;
        }
    };
    if host == "0.0.0.0" {
        host = "127.0.0.1".to_string();
    }

    match action {
        SystemAction::On => {
            for args in system_proxy::set_commands(&svc, &host, port) {
                let _ = system_proxy::networksetup(&args);
            }
            println!("系统代理（{}）已指向 {}:{}", svc, host, port);
        }
        SystemAction::Off => {
            for args in system_proxy::off_commands(&svc) {
                let _ = system_proxy::networksetup(&args);
            }
            println!("系统代理（{}）已关闭", svc);
        }
        SystemAction::Status => {
            for (kind, flag) in [
                ("HTTP", "-getwebproxy"),
                ("HTTPS", "-getsecurewebproxy"),
                ("SOCKS", "-getsocksfirewallproxy"),
            ] {
                let data = system_proxy::get_proxy(flag, &svc).unwrap_or_default();
                let get = |key: &str| {
                    data.iter()
                        .find(|(k, _)| k == key)
                        .map(|(_, v)| v.clone())
                        .unwrap_or_default()
                };
                let state = if get("Enabled") == "Yes" { "开" } else { "关" };
                println!("  {:<6}{}  {}:{}", kind, state, get("Server"), get("Port"));
            }
        }
        // 上面已经提前返回，这里只是为了穷尽匹配
        SystemAction::Use { .. } => {}
    }
    0
}
