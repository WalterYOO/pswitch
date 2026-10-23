"""TCP 转发守护进程：一个本地端口，多个上游。"""

import asyncio
import logging
import os
import re
import signal
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit

from .config import (
    HOME,
    LOG_PATH,
    PID_PATH,
    STATE_PATH,
    config_hint,
    enabled_upstreams,
    load_config,
    split_hostport,
    write_json,
)

log = logging.getLogger("pswitch")


# --------------------------------------------------------------------------
# 进程 / 端口辅助
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# 探活
# --------------------------------------------------------------------------

async def _probe(addr, timeout=1.0):
    """TCP 探测，返回 (ok, 毫秒)。"""
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


async def probe_full(addr, url, timeout=3.0):
    """HTTP 探活优先；只通 TCP 的上游标记为 tcp-only，排序时靠后。"""
    ok_http, ms = await _probe_http(addr, url, timeout)
    if ok_http:
        return {"tcp": True, "http": True, "ms": ms}
    ok_tcp, ms_tcp = await _probe(addr, 1.0)
    return {"tcp": ok_tcp, "http": False, "ms": ms_tcp if ok_tcp else None}


# --------------------------------------------------------------------------
# 转发
# --------------------------------------------------------------------------

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
            return HOME.joinpath("config.json").stat().st_mtime
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
        # 被 disable 的上游完全不参与选路（manual 指向它时也自动落回 auto 顺序）
        ups = enabled_upstreams(self.cfg)
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
        ups = enabled_upstreams(self.cfg)
        url = self.cfg.get("probe_url")
        timeout = self.cfg.get("probe_timeout", 3)
        results = await asyncio.gather(*[probe_full(u["addr"], url, timeout) for u in ups])
        # 整体替换，避免已禁用上游残留过期的健康状态
        self.health = {u["name"]: h for u, h in zip(ups, results)}
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
                for u in enabled_upstreams(self.cfg):
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


# --------------------------------------------------------------------------
# 启动入口
# --------------------------------------------------------------------------

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
    cfg = load_config()
    if not enabled_upstreams(cfg):
        if cfg.get("upstreams"):
            msg = ("所有上游都被禁用了，没有可用上游，拒绝启动。\n"
                   "先启用一个，例如: pswitch enable <名字>\n"
                   + config_hint())
            log.error("所有上游都被禁用，拒绝启动")
        else:
            msg = ("没有配置任何上游，拒绝启动。\n"
                   "先添加一个，例如: pswitch add local 127.0.0.1:17890\n"
                   + config_hint())
            log.error("没有配置任何上游，拒绝启动")
        print(msg, file=sys.stderr)
        return 2
    if daemon_running():
        log.error("已有实例在运行 (pid %s)", read_pid())
        return 1
    proxy = ProxyServer(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, proxy._stop.set)
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, proxy.reload)
    await proxy.run()
    return 0


def serve_main(foreground=False, debug=False):
    try:
        return asyncio.run(_serve(foreground, debug))
    except KeyboardInterrupt:
        return 0
