#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pswitch — 一个本地端口，多个上游代理，随时切换。

所有客户端（curl / git / npm / 浏览器 / GUI 应用）永远只连 127.0.0.1:17000。
切换上游只需 `pswitch use <名字>`，不用改环境变量。

TCP 层透明转发，所以同一个本地端口既能当 HTTP 代理用，也能当 SOCKS5 用，
与上游是什么协议无关。
"""

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path(os.environ.get("PSWITCH_HOME") or (Path.home() / ".config" / "proxy_switch"))
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"
PID_PATH = HOME / "pswitch.pid"
LOG_PATH = HOME / "pswitch.log"
SCRIPT = Path(__file__).resolve()
LAUNCHD_LABEL = f"com.{getpass.getuser()}.pswitch"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

DEFAULT_CONFIG = {
    "listen": "127.0.0.1:17000",
    "mode": "auto",                       # auto = 按列表顺序取第一个可用的；manual = 固定 active
    "active": "local",                    # manual 模式下的上游名字
    "health_check_interval": 10,          # 秒
    "connect_timeout": 3,                 # 连接上游超时（秒）
    "failover": True,                     # 当前上游连不上时临时借用其它可用上游
    "probe_url": "http://connectivitycheck.gstatic.com/generate_204",
    "probe_timeout": 3,                   # HTTP 探活超时（秒）
    "upstreams": [
        {"name": "local", "addr": "127.0.0.1:17890"},
        {"name": "lan-17090", "addr": "192.168.212.21:17090"},
        {"name": "lan-7890", "addr": "192.168.212.21:7890"},
    ],
}

log = logging.getLogger("pswitch")


# --------------------------------------------------------------------------
# 配置 / 状态读写
# --------------------------------------------------------------------------

def split_hostport(s):
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"地址格式应为 host:port，收到 {s!r}")
    return host, int(port)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_config():
    HOME.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        write_json(CONFIG_PATH, DEFAULT_CONFIG)


def load_config():
    ensure_config()
    cfg = read_json(CONFIG_PATH) or {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(cfg)
    if not merged.get("upstreams"):
        merged["upstreams"] = list(DEFAULT_CONFIG["upstreams"])
    return merged


def save_config(cfg):
    write_json(CONFIG_PATH, cfg)


def read_pid():
    try:
        return int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_running():
    pid = read_pid()
    return pid if pid_alive(pid) else None


def port_open(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def signal_daemon(sig):
    pid = daemon_running()
    if pid:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 转发守护进程
# --------------------------------------------------------------------------

async def _probe(addr, timeout=1.0):
    """TCP 探测上游是否可用，返回 (ok, 毫秒)。"""
    try:
        host, port = split_hostport(addr)
        t0 = time.monotonic()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, round((time.monotonic() - t0) * 1000)
    except Exception:
        return False, None


async def _probe_http(addr, url, timeout=3.0):
    """以 HTTP 代理方式真实请求一次，验证上游是否真能转发。返回 (ok, 毫秒)。"""
    try:
        host, port = split_hostport(addr)
        u = urlsplit(url)
        host_hdr = u.hostname + (f":{u.port}" if u.port else "")
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        req = (
            f"GET {u.scheme}://{host_hdr}{path} HTTP/1.1\r\n"
            f"Host: {host_hdr}\r\n"
            "User-Agent: pswitch-health\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        t0 = time.monotonic()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        try:
            writer.write(req)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        m = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", head)
        ok = bool(m) and int(m.group(1)) in (200, 204)
        return ok, round((time.monotonic() - t0) * 1000)
    except Exception:
        return False, None


async def _probe_full(addr, url, timeout=3.0):
    """HTTP 探活优先；只通 TCP 的上游标记为 tcp-only，排序时靠后。"""
    ok_http, ms = await _probe_http(addr, url, timeout)
    if ok_http:
        return {"tcp": True, "http": True, "ms": ms}
    ok_tcp, ms_tcp = await _probe(addr, 1.0)
    return {"tcp": ok_tcp, "http": False, "ms": ms_tcp if ok_tcp else None}


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class ProxyServer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.health = {}
        self.active_upstream = None
        self._stop = asyncio.Event()
        self._config_mtime = self._mtime()

    # -- 配置 ------------------------------------------------------------
    @staticmethod
    def _mtime():
        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return 0

    def reload(self):
        try:
            new = load_config()
        except Exception as e:
            log.error("重载配置失败: %s", e)
            return
        if new["listen"] != self.cfg["listen"]:
            log.warning("监听地址变化需要 restart 才生效，暂沿用 %s", self.cfg["listen"])
            new["listen"] = self.cfg["listen"]
        self.cfg = new
        self._config_mtime = self._mtime()
        log.info("配置已重载: mode=%s active=%s", new["mode"], new.get("active"))
        self._recompute()
        self._write_state()

    # -- 选路 ------------------------------------------------------------
    def _pick(self):
        ups = self.cfg["upstreams"]
        if not ups:
            return None
        if self.cfg.get("mode") == "manual":
            name = self.cfg.get("active")
            for u in ups:
                if u["name"] == name:
                    return u
        for key in ("http", "tcp"):
            for u in ups:
                if self.health.get(u["name"], {}).get(key):
                    return u
        return ups[0]

    def _recompute(self):
        old = (self.active_upstream or {}).get("name")
        self.active_upstream = self._pick()
        new = (self.active_upstream or {}).get("name")
        if new != old:
            if self.active_upstream:
                log.info("当前上游: %s -> %s (%s)", old, new, self.active_upstream["addr"])
            else:
                log.warning("当前上游: %s -> 无", old)

    @property
    def _health_interval(self):
        try:
            return max(2, int(self.cfg.get("health_check_interval", 10)))
        except (TypeError, ValueError):
            return 10

    async def _check_all(self):
        ups = self.cfg["upstreams"]
        url = self.cfg.get("probe_url")
        timeout = self.cfg.get("probe_timeout", 3)
        results = await asyncio.gather(*[_probe_full(u["addr"], url, timeout) for u in ups])
        for u, h in zip(ups, results):
            self.health[u["name"]] = h
        self._recompute()
        self._write_state()

    def _write_state(self):
        up = self.active_upstream or {}
        state = {
            "pid": os.getpid(),
            "listen": self.cfg["listen"],
            "mode": self.cfg.get("mode"),
            "manual": self.cfg.get("active"),
            "active": up.get("name"),
            "active_addr": up.get("addr"),
            "health": self.health,
            "updated": time.time(),
        }
        try:
            write_json(STATE_PATH, state)
        except OSError as e:
            log.warning("写 state 失败: %s", e)

    async def _health_loop(self):
        while not self._stop.is_set():
            try:
                await self._check_all()
                mtime = self._mtime()
                if mtime != self._config_mtime:
                    self.reload()
            except Exception as e:
                log.exception("健康检查异常: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._health_interval)
            except asyncio.TimeoutError:
                pass

    # -- 连接处理 --------------------------------------------------------
    async def handle(self, creader, cwriter):
        peer = cwriter.get_extra_info("peername")
        up = self.active_upstream
        if not up:
            cwriter.close()
            return

        candidates = [up]
        if self.cfg.get("failover", True):
            for key in ("http", "tcp"):
                for u in self.cfg["upstreams"]:
                    if (u["name"] != up["name"] and u not in candidates
                            and self.health.get(u["name"], {}).get(key)):
                        candidates.append(u)

        ureader = uwriter = None
        for cand in candidates:
            try:
                host, port = split_hostport(cand["addr"])
                ureader, uwriter = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self.cfg.get("connect_timeout", 3))
                if cand is not up:
                    log.warning("上游 %s 不可用，临时改走 %s", up["name"], cand["name"])
                else:
                    log.debug("%s -> %s (%s)", peer, cand["name"], cand["addr"])
                break
            except Exception as e:
                log.warning("连接上游 %s (%s) 失败: %s", cand["name"], cand["addr"], e)

        if ureader is None:
            log.error("所有上游都连不上，断开客户端 %s", peer)
            cwriter.close()
            return

        try:
            await asyncio.gather(
                _pipe(creader, uwriter),
                _pipe(ureader, cwriter),
            )
        finally:
            for w in (uwriter, cwriter):
                try:
                    w.close()
                except Exception:
                    pass

    async def run(self):
        host, port = split_hostport(self.cfg["listen"])
        # 先完成首轮探活再开放端口，避免刚启动时选错上游
        await self._check_all()
        server = await asyncio.start_server(self.handle, host, port, reuse_address=True)
        PID_PATH.write_text(str(os.getpid()))
        log.info("pswitch 已启动，监听 %s:%s，模式 %s，当前上游 %s",
                 host, port, self.cfg.get("mode"),
                 (self.active_upstream or {}).get("name"))
        health_task = asyncio.create_task(self._health_loop())
        serve_task = asyncio.create_task(server.serve_forever())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (health_task, serve_task):
                t.cancel()
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
            try:
                PID_PATH.unlink()
            except OSError:
                pass
            log.info("pswitch 已停止")


def setup_logging(foreground=False, debug=False):
    HOME.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if foreground:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


async def _serve(foreground, debug):
    setup_logging(foreground, debug)
    if daemon_running():
        log.error("已有实例在运行 (pid %s)", read_pid())
        return 1
    proxy = ProxyServer(load_config())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, proxy._stop.set)
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, proxy.reload)
    await proxy.run()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def find_upstream(cfg, key):
    for u in cfg["upstreams"]:
        if u["name"] == key or u["addr"] == key:
            return u
    return None


def cmd_start(args):
    pid = daemon_running()
    if pid:
        print(f"已经在运行 (pid {pid})")
        return 0
    HOME.mkdir(parents=True, exist_ok=True)
    host, port = split_hostport(load_config()["listen"])

    if launchd_installed():
        r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(LAUNCHD_PLIST))
        if r.returncode != 0 and "already bootstrapped" not in (r.stderr or ""):
            print(f"launchctl 启动失败: {(r.stderr or '').strip()}")
            return 1
    else:
        out = open(LOG_PATH, "a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, str(SCRIPT), "serve"],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True, close_fds=True)

    for _ in range(60):
        time.sleep(0.1)
        pid = daemon_running()
        if pid and port_open(host, port):
            print(f"已启动 (pid {pid})，监听 {host}:{port}")
            return 0
    print(f"启动失败或超时，看日志: {LOG_PATH}")
    return 1


def cmd_stop(args):
    pid = daemon_running()
    if launchd_installed():
        # 先摘掉 launchd 作业，否则 KeepAlive 会立刻把它拉起来
        _launchctl("bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST))
    if not pid:
        print("没有在运行")
        return 0
    for _ in range(50):
        if not pid_alive(pid):
            print("已停止")
            return 0
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
        print("已强制停止")
    except OSError:
        print("进程已退出")
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    return 0


def cmd_restart(args):
    cmd_stop(args)
    return cmd_start(args)


def _live_probe(cfg):
    async def _all():
        url = cfg.get("probe_url")
        timeout = cfg.get("probe_timeout", 3)
        return await asyncio.gather(*[_probe_full(u["addr"], url, timeout)
                                      for u in cfg["upstreams"]])
    return asyncio.run(_all())


def cmd_status(args):
    cfg = load_config()
    st = read_json(STATE_PATH) or {}
    pid = daemon_running()
    host, port = split_hostport(cfg["listen"])
    listening = port_open(host, port) if pid else False
    probes = _live_probe(cfg)

    mode = cfg.get("mode")
    interval = max(2, int(cfg.get("health_check_interval", 10) or 10))
    fresh = pid and (time.time() - st.get("updated", 0)) < max(15, 3 * interval)
    if fresh and st.get("mode") == mode:
        active = st.get("active")
    elif mode == "manual":
        active = (find_upstream(cfg, cfg.get("active")) or {}).get("name")
    else:
        active = next((u["name"] for u, h in zip(cfg["upstreams"], probes) if h["http"]), None)
        if not active:
            active = next((u["name"] for u, h in zip(cfg["upstreams"], probes) if h["tcp"]), None)
        if not active and cfg["upstreams"]:
            active = cfg["upstreams"][0]["name"]

    print("pswitch")
    if pid:
        print(f"  守护进程  运行中 (pid {pid})")
    else:
        print("  守护进程  未运行  → pswitch start")
    print(f"  监听      {cfg['listen']}  {'可连接' if listening else '不可连接'}")
    print(f"  模式      {'manual 固定' if mode == 'manual' else 'auto 自动选择'}")
    print(f"  当前上游  {active or '-'}")
    print("  上游列表")
    for u, h in zip(cfg["upstreams"], probes):
        mark = "*" if u["name"] == active else " "
        if h["http"]:
            state = f"OK        {h['ms'] or 0:>3}ms"
        elif h["tcp"]:
            state = "TCP only"
        else:
            state = "DOWN"
        print(f"   {mark} {u['name']:<12} {u['addr']:<24} {state}")
    return 0


def cmd_use(args):
    cfg = load_config()
    key = args.target
    if key == "auto":
        cfg["mode"] = "auto"
        save_config(cfg)
        signal_daemon(signal.SIGHUP)
        print("已切换为 auto：按顺序自动选择第一个可用的上游")
        return 0
    u = find_upstream(cfg, key)
    if not u:
        names = ", ".join(x["name"] for x in cfg["upstreams"])
        print(f"没有这个上游: {key}\n可选: {names}, 或 auto")
        return 1
    cfg["mode"] = "manual"
    cfg["active"] = u["name"]
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    print(f"已切换: {u['name']} -> {u['addr']}")
    return 0


def cmd_list(args):
    cfg = load_config()
    active = (read_json(STATE_PATH) or {}).get("active")
    for i, u in enumerate(cfg["upstreams"], 1):
        mark = "*" if u["name"] == active else " "
        print(f" {mark} {i}. {u['name']:<12} {u['addr']}")
    return 0


def cmd_add(args):
    cfg = load_config()
    split_hostport(args.addr)  # 校验
    if find_upstream(cfg, args.name):
        print(f"名字已存在: {args.name}")
        return 1
    cfg["upstreams"].append({"name": args.name, "addr": args.addr})
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    print(f"已添加: {args.name} -> {args.addr}")
    return 0


def cmd_remove(args):
    cfg = load_config()
    before = len(cfg["upstreams"])
    cfg["upstreams"] = [u for u in cfg["upstreams"] if u["name"] != args.name]
    if len(cfg["upstreams"]) == before:
        print(f"没有这个上游: {args.name}")
        return 1
    if cfg.get("active") == args.name:
        cfg["mode"] = "auto"
        cfg["active"] = cfg["upstreams"][0]["name"] if cfg["upstreams"] else None
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    print(f"已删除: {args.name}")
    return 0


def cmd_env(args):
    cfg = load_config()
    proxy = f"http://{cfg['listen']}"
    socks = f"socks5://{cfg['listen']}"
    for line in (
        f'export http_proxy="{proxy}"',
        f'export https_proxy="{proxy}"',
        f'export HTTP_PROXY="{proxy}"',
        f'export HTTPS_PROXY="{proxy}"',
        f'export all_proxy="{socks}"',
        f'export ALL_PROXY="{socks}"',
        'export no_proxy="127.0.0.1,localhost"',
        'export NO_PROXY="127.0.0.1,localhost"',
    ):
        print(line)
    return 0


def cmd_test(args):
    cfg = load_config()
    host, port = split_hostport(cfg["listen"])
    proxy = f"http://{host}:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    ok_all = True
    for url in ("http://connectivitycheck.gstatic.com/generate_204",
                "https://www.gstatic.com/generate_204"):
        t0 = time.monotonic()
        try:
            with opener.open(url, timeout=args.timeout) as r:
                code = r.status
            print(f"OK   {url}  {code}  {round((time.monotonic()-t0)*1000)}ms")
        except Exception as e:
            ok_all = False
            print(f"FAIL {url}  {e}")
    st = read_json(STATE_PATH) or {}
    if st.get("active"):
        print(f"实际出口: {st['active']} ({st.get('active_addr')})")
    return 0 if ok_all else 1


def cmd_logs(args):
    if not LOG_PATH.exists():
        print(f"暂无日志: {LOG_PATH}")
        return 0
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)
    print(f"\n(日志文件: {LOG_PATH}，实时跟踪: tail -f {LOG_PATH})")
    return 0


# -- launchd ---------------------------------------------------------------

def launchd_installed():
    return sys.platform == "darwin" and LAUNCHD_PLIST.exists()


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _plist_text():
    python = shutil.which("python3") or sys.executable
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{SCRIPT}</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{LOG_PATH}</string>
  <key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict>
</plist>
"""


def cmd_install(args):
    if sys.platform != "darwin":
        print("仅支持 macOS 的 launchd")
        return 1
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.write_text(_plist_text(), encoding="utf-8")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0 and "already bootstrapped" not in (r.stderr or ""):
        print(f"launchctl 失败: {(r.stderr or '').strip()}")
        return 1
    host, port = split_hostport(load_config()["listen"])
    for _ in range(100):
        pid = daemon_running()
        if pid and port_open(host, port):
            print(f"已安装开机自启并启动 (pid {pid})，监听 {host}:{port}")
            print(f"plist: {LAUNCHD_PLIST}")
            print("停止并禁用: pswitch uninstall")
            return 0
        time.sleep(0.1)
    print(f"已安装开机自启: {LAUNCHD_PLIST}（进程尚未就绪，看日志 {LOG_PATH}）")
    print("停止并禁用: pswitch uninstall")
    return 0


def cmd_uninstall(args):
    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCHD_PLIST)],
                       capture_output=True)
        LAUNCHD_PLIST.unlink()
    cmd_stop(args)
    print("已卸载开机自启")
    return 0


# -- macOS 系统代理 ---------------------------------------------------------

def active_network_service():
    try:
        out = subprocess.run(["route", "-n", "get", "default"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    iface = None
    for line in out.splitlines():
        if "interface:" in line:
            iface = line.split("interface:")[1].strip()
            break
    if not iface:
        return None
    try:
        order = subprocess.run(["networksetup", "-listnetworkserviceorder"],
                               capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    svc = None
    for line in order.splitlines():
        m = re.match(r"^\((?:\*)?\d+\)\s+(.+)$", line.strip())
        if m:
            svc = m.group(1).strip()
            continue
        if svc and "Device:" in line:
            dev = line.split("Device:")[1].split(")")[0].strip()
            if dev == iface:
                return svc
    return None


def cmd_system(args):
    svc = active_network_service()
    if not svc:
        print("找不到当前活动的网络服务")
        return 1
    host, port = split_hostport(load_config()["listen"])
    if host == "0.0.0.0":
        host = "127.0.0.1"
    sets = [
        ["-setwebproxy", svc, host, str(port)],
        ["-setsecurewebproxy", svc, host, str(port)],
        ["-setsocksfirewallproxy", svc, host, str(port)],
        ["-setproxybypassdomains", svc, "127.0.0.1", "localhost"],
    ]
    offs = [
        ["-setwebproxystate", svc, "off"],
        ["-setsecurewebproxystate", svc, "off"],
        ["-setsocksfirewallproxystate", svc, "off"],
    ]
    if args.action == "on":
        for cmd in sets:
            subprocess.run(["networksetup", *cmd], capture_output=True)
        print(f"系统代理（{svc}）已指向 {host}:{port}")
    elif args.action == "off":
        for cmd in offs:
            subprocess.run(["networksetup", *cmd], capture_output=True)
        print(f"系统代理（{svc}）已关闭")
    else:
        for kind, flag in (("HTTP", "-getwebproxy"),
                           ("HTTPS", "-getsecurewebproxy"),
                           ("SOCKS", "-getsocksfirewallproxy")):
            r = subprocess.run(["networksetup", flag, svc],
                               capture_output=True, text=True)
            data = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
            state = "开" if data.get("Enabled", "").strip() == "Yes" else "关"
            print(f"  {kind:<6}{state}  {data.get('Server','').strip()}:{data.get('Port','').strip()}")
    return 0


# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="pswitch", description="一个本地端口，多个上游代理")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help=argparse.SUPPRESS)
    s.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("start", help="启动转发守护进程")
    sub.add_parser("stop", help="停止守护进程")
    sub.add_parser("restart", help="重启守护进程")
    sub.add_parser("status", help="查看状态（含实时可达性）")
    sub.add_parser("list", help="列出上游")
    sub.add_parser("env", help="打印应设置的代理环境变量")
    sub.add_parser("install", help="安装为 launchd 开机自启")
    sub.add_parser("uninstall", help="卸载开机自启")

    u = sub.add_parser("use", help="切换上游：use <名字> | use auto")
    u.add_argument("target")

    a = sub.add_parser("add", help="添加上游")
    a.add_argument("name")
    a.add_argument("addr")

    r = sub.add_parser("remove", help="删除上游")
    r.add_argument("name")

    t = sub.add_parser("test", help="经本地端口做一次真实请求")
    t.add_argument("--timeout", type=float, default=8)

    lg = sub.add_parser("logs", help="查看最近日志")
    lg.add_argument("-n", "--lines", type=int, default=30)

    sy = sub.add_parser("system", help="管理 macOS 系统代理指向")
    sy.add_argument("action", choices=["on", "off", "status"])

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        try:
            return asyncio.run(_serve(args.foreground, args.debug))
        except KeyboardInterrupt:
            return 0
    handlers = {
        "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
        "status": cmd_status, "use": cmd_use, "list": cmd_list,
        "add": cmd_add, "remove": cmd_remove, "env": cmd_env,
        "test": cmd_test, "logs": cmd_logs,
        "install": cmd_install, "uninstall": cmd_uninstall,
        "system": cmd_system,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
