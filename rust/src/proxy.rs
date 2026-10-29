//! TCP 转发守护进程：一个本地端口，多个上游。

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;
use tokio::time::sleep;

use crate::config::{self, Config, Health, State, Upstream};
use crate::direct;
use crate::relay;
use crate::util;

/// 连接处理用的只读路由快照：热路径只做一次 RwLock 读 + Arc clone。
#[derive(Clone)]
pub struct Route {
    pub direct: bool,
    pub active: Option<Upstream>,
    pub candidates: Vec<Upstream>,
    pub failover: bool,
    pub connect_timeout: Duration,
    pub allow: Vec<util::IpNet>,
    pub deny: Vec<util::IpNet>,
}

pub struct Server {
    cfg: Config,
    health: HashMap<String, Health>,
    last_active: Option<String>,
    shared: Arc<RwLock<Arc<Route>>>,
    config_mtime: Option<SystemTime>,
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn read_shared(store: &RwLock<Arc<Route>>) -> Arc<Route> {
    store.read().unwrap_or_else(|e| e.into_inner()).clone()
}

impl Server {
    fn new(cfg: Config) -> Self {
        let initial = Route {
            direct: cfg.is_direct(),
            active: None,
            candidates: Vec::new(),
            failover: cfg.failover,
            connect_timeout: cfg.connect_timeout(),
            allow: util::parse_allow(&cfg.allow_cidrs),
            deny: util::parse_allow(&cfg.deny_cidrs),
        };
        Server {
            config_mtime: config::config_mtime(),
            cfg,
            health: HashMap::new(),
            last_active: None,
            shared: Arc::new(RwLock::new(Arc::new(initial))),
        }
    }

    // -- 选路 ------------------------------------------------------------
    fn make_route(&mut self) -> Route {
        if self.cfg.is_direct() {
            if self.last_active.is_some() {
                log_info!("当前模式: direct（不走任何上游，全部直连）");
            }
            self.last_active = None;
            return Route {
                direct: true,
                active: None,
                candidates: Vec::new(),
                failover: self.cfg.failover,
                connect_timeout: self.cfg.connect_timeout(),
                allow: util::parse_allow(&self.cfg.allow_cidrs),
                deny: util::parse_allow(&self.cfg.deny_cidrs),
            };
        }

        let active = pick(&self.cfg, &self.health);
        let new = active.as_ref().map(|u| u.name.clone());
        if new != self.last_active {
            match &active {
                Some(u) => log_info!(
                    "当前上游: {} -> {} ({})",
                    self.last_active.as_deref().unwrap_or("None"),
                    u.name,
                    u.addr
                ),
                None => log_warn!(
                    "当前上游: {} -> 无",
                    self.last_active.as_deref().unwrap_or("None")
                ),
            }
        }
        self.last_active = new;

        let candidates = if self.cfg.failover {
            fallback_candidates(&self.cfg, &self.health, active.as_ref())
        } else {
            Vec::new()
        };
        Route {
            direct: false,
            active,
            candidates,
            failover: self.cfg.failover,
            connect_timeout: self.cfg.connect_timeout(),
            allow: util::parse_allow(&self.cfg.allow_cidrs),
            deny: util::parse_allow(&self.cfg.deny_cidrs),
        }
    }

    fn publish_route(&mut self) {
        let route = Arc::new(self.make_route());
        *self.shared.write().unwrap_or_else(|e| e.into_inner()) = route;
    }

    fn write_state(&self) {
        let route = read_shared(&self.shared);
        let state = State {
            pid: std::process::id(),
            listen: self.cfg.listen.clone(),
            mode: self.cfg.mode.clone(),
            manual: self.cfg.active.clone(),
            active: route.active.as_ref().map(|u| u.name.clone()),
            active_addr: route.active.as_ref().map(|u| u.addr.clone()),
            health: self.health.clone(),
            updated: now_secs(),
        };
        config::write_state(&state);
    }

    // -- 探活 ------------------------------------------------------------
    async fn check_all(&mut self) {
        if self.cfg.is_direct() {
            // direct 模式不依赖上游，跳过探活
            self.health.clear();
            self.publish_route();
            self.write_state();
            return;
        }
        let ups = config::enabled_upstreams(&self.cfg);
        let url = self.cfg.probe_url.clone();
        let timeout = self.cfg.probe_timeout;

        // 并发探测，按配置顺序收集结果
        let mut handles = Vec::with_capacity(ups.len());
        for u in &ups {
            let addr = u.addr.clone();
            let url = url.clone();
            handles.push(tokio::spawn(async move {
                crate::probe::probe_full(&addr, &url, timeout).await
            }));
        }
        let mut health = HashMap::with_capacity(ups.len());
        for (u, h) in ups.iter().zip(handles) {
            health.insert(u.name.clone(), h.await.unwrap_or_default());
        }
        self.health = health;
        self.publish_route();
        self.write_state();
    }

    /// 重载配置。返回 true 表示需要立刻做一轮探活再选路。
    fn reload(&mut self) -> bool {
        let mut new = config::load_config();
        if new.listen != self.cfg.listen {
            log_warn!(
                "监听地址变化需要 restart 才生效，暂沿用 {}",
                self.cfg.listen
            );
            new.listen = self.cfg.listen.clone();
        }
        self.cfg = new;
        self.config_mtime = config::config_mtime();
        log_info!(
            "配置已重载: mode={} active={}",
            self.cfg.mode,
            self.cfg.active.as_deref().unwrap_or("None")
        );
        if !self.cfg.is_direct() && self.health.is_empty() {
            true
        } else {
            self.publish_route();
            self.write_state();
            false
        }
    }

    async fn health_loop(
        mut self,
        mut stop: watch::Receiver<bool>,
        mut reload: watch::Receiver<u64>,
    ) {
        loop {
            self.check_all().await;

            let mtime = config::config_mtime();
            if mtime != self.config_mtime && self.reload() {
                self.check_all().await;
            }

            let wait = Duration::from_secs(self.cfg.interval() as u64);
            tokio::select! {
                _ = stop.changed() => break,
                _ = reload.changed() => {
                    if self.reload() {
                        self.check_all().await;
                    }
                }
                _ = sleep(wait) => {}
            }
        }
    }
}

// --------------------------------------------------------------------------
// 选路
// --------------------------------------------------------------------------

fn health_ok(health: &HashMap<String, Health>, name: &str, http: bool) -> bool {
    match health.get(name) {
        Some(h) => {
            if http {
                h.http
            } else {
                h.tcp
            }
        }
        None => false,
    }
}

fn pick(cfg: &Config, health: &HashMap<String, Health>) -> Option<Upstream> {
    if cfg.is_direct() {
        return None;
    }
    let ups = config::enabled_upstreams(cfg);
    if ups.is_empty() {
        return None;
    }
    if cfg.is_manual() {
        if let Some(name) = cfg.active.as_deref() {
            if let Some(u) = ups.iter().find(|u| u.name == name) {
                return Some(u.clone());
            }
        }
    }
    for http in [true, false] {
        for u in &ups {
            if health_ok(health, &u.name, http) {
                return Some(u.clone());
            }
        }
    }
    Some(ups[0].clone())
}

fn fallback_candidates(
    cfg: &Config,
    health: &HashMap<String, Health>,
    active: Option<&Upstream>,
) -> Vec<Upstream> {
    let mut out: Vec<Upstream> = Vec::new();
    let Some(active) = active else {
        return out;
    };
    for http in [true, false] {
        for u in config::enabled_upstreams(cfg) {
            if u.name == active.name || out.iter().any(|x| x.name == u.name) {
                continue;
            }
            if health_ok(health, &u.name, http) {
                out.push(u);
            }
        }
    }
    out
}

// --------------------------------------------------------------------------
// 连接处理
// --------------------------------------------------------------------------

pub async fn handle_client(client: TcpStream, peer: SocketAddr, route: Arc<Route>) {
    let _ = client.set_nodelay(true);

    if !util::peer_allowed(&route.allow, &route.deny, peer.ip()) {
        log_warn!("拒绝来自 {} 的连接：不在允许来源列表中", peer);
        if let Ok(std_stream) = client.into_std() {
            let _ = std_stream.shutdown(std::net::Shutdown::Both);
        }
        return;
    }

    if route.direct {
        direct::handle(client, route.connect_timeout, peer).await;
        return;
    }

    let Some(active) = route.active.clone() else {
        return;
    };

    let mut candidates = Vec::with_capacity(1 + route.candidates.len());
    candidates.push(active.clone());
    if route.failover {
        candidates.extend(route.candidates.iter().cloned());
    }

    let mut upstream: Option<TcpStream> = None;
    let mut tried: Vec<&str> = Vec::with_capacity(candidates.len());
    for cand in &candidates {
        if tried.contains(&cand.name.as_str()) {
            continue;
        }
        tried.push(cand.name.as_str());
        match util::connect_tcp(&cand.addr, route.connect_timeout).await {
            Ok(stream) => {
                if cand.name != active.name {
                    log_warn!("上游 {} 不可用，临时改走 {}", active.name, cand.name);
                } else {
                    log_debug!("{} -> {} ({})", peer, cand.name, cand.addr);
                }
                upstream = Some(stream);
                break;
            }
            Err(e) => {
                log_warn!("连接上游 {} ({}) 失败: {}", cand.name, cand.addr, e);
            }
        }
    }

    let Some(upstream) = upstream else {
        log_error!("所有上游都连不上，断开客户端 {}", peer);
        // 先 shutdown 再 drop：客户端可能已经发来数据，直接 close 会变成 RST，
        // 而代理应该让调用方看到干净的 EOF。
        if let Ok(std_stream) = client.into_std() {
            let _ = std_stream.shutdown(std::net::Shutdown::Both);
        }
        return;
    };

    relay::relay(client, upstream, Vec::new(), Vec::new()).await;
}

// --------------------------------------------------------------------------
// 启动入口
// --------------------------------------------------------------------------

async fn signal_task(stop: watch::Sender<bool>, reload: watch::Sender<u64>) {
    use tokio::signal::unix::{signal, SignalKind};
    let (Ok(mut term), Ok(mut int), Ok(mut hup)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
        signal(SignalKind::hangup()),
    ) else {
        return;
    };
    loop {
        tokio::select! {
            _ = term.recv() => {
                let _ = stop.send(true);
                break;
            }
            _ = int.recv() => {
                let _ = stop.send(true);
                break;
            }
            _ = hup.recv() => {
                let v = *reload.borrow();
                let _ = reload.send(v.wrapping_add(1));
            }
        }
    }
}

pub async fn serve_main() -> i32 {
    let cfg = config::load_config();

    // direct 模式不依赖上游，允许零上游启动
    if !cfg.is_direct() && config::enabled_upstreams(&cfg).is_empty() {
        let msg = if cfg.upstreams.is_empty() {
            log_error!("没有配置任何上游，拒绝启动");
            "没有配置任何上游，拒绝启动。\n先添加一个，例如: pswitch add local 127.0.0.1:17890"
        } else {
            log_error!("所有上游都被禁用，拒绝启动");
            "所有上游都被禁用了，没有可用上游，拒绝启动。\n先启用一个，例如: pswitch enable <名字>"
        };
        eprintln!("{}\n{}", msg, config::config_hint());
        return 2;
    }

    if let Some(pid) = util::daemon_running() {
        log_error!("已有实例在运行 (pid {})", pid);
        return 1;
    }

    let mut server = Server::new(cfg);
    // 先完成首轮探活再开放端口，避免刚启动时选错上游
    server.check_all().await;

    let listen = server.cfg.listen.clone();
    let listener = match TcpListener::bind(&listen).await {
        Ok(l) => l,
        Err(e) => {
            log_error!("监听 {} 失败: {}", listen, e);
            return 1;
        }
    };
    let _ = std::fs::write(config::pid_path(), std::process::id().to_string());

    {
        let route = read_shared(&server.shared);
        if route.direct {
            log_info!("pswitch 已启动，监听 {}，模式 direct（全部直连）", listen);
        } else {
            let active = route
                .active
                .as_ref()
                .map(|u| u.name.clone())
                .unwrap_or_else(|| "None".to_string());
            log_info!(
                "pswitch 已启动，监听 {}，模式 {}，当前上游 {}",
                listen,
                server.cfg.mode,
                active
            );
        }
    }

    if let Ok((host, _)) = util::split_hostport(&listen) {
        if !util::is_loopback_host(&host) {
            let allow = if server.cfg.allow_cidrs.is_empty() {
                "无（仅本机）".to_string()
            } else {
                server.cfg.allow_cidrs.join(", ")
            };
            log_info!("允许来源（LAN）: {}", allow);
            if !server.cfg.deny_cidrs.is_empty() {
                log_info!("拒绝来源（LAN）: {}", server.cfg.deny_cidrs.join(", "));
            }
        }
    }

    let (stop_tx, stop_rx) = watch::channel(false);
    let (reload_tx, reload_rx) = watch::channel(0u64);
    tokio::spawn(signal_task(stop_tx, reload_tx));

    let route_store = server.shared.clone();
    let mut stop_accept = stop_rx.clone();
    let health_handle = tokio::spawn(async move {
        server.health_loop(stop_rx, reload_rx).await;
    });

    loop {
        tokio::select! {
            _ = stop_accept.changed() => break,
            accepted = listener.accept() => {
                match accepted {
                    Ok((stream, peer)) => {
                        let route = read_shared(&route_store);
                        tokio::spawn(async move {
                            handle_client(stream, peer, route).await;
                        });
                    }
                    Err(e) => {
                        // EMFILE 之类：别空转
                        log_warn!("accept 失败: {}", e);
                        sleep(Duration::from_millis(10)).await;
                    }
                }
            }
        }
    }

    health_handle.abort();
    drop(listener);
    let _ = std::fs::remove_file(config::pid_path());
    log_info!("pswitch 已停止");
    0
}
