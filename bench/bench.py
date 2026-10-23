#!/usr/bin/env python3
"""pswitch 统一延迟基准。

设计目标：任何人、任何一次运行，都能用同一条命令得到可对比的结果。

  * 所有测试目标都是本地 test double，不依赖公网和真实上游；
  * 每个 pswitch 实例使用独立的 PSWITCH_HOME 和 17100+ 端口，
    绝不碰正在运行的 127.0.0.1:17000；
  * 结果同时写 JSON（机器可读）和 Markdown（人可读）。

用法见 bench/README.md，最常用的是：

    python bench/bench.py all

单测某一项：

    python bench/bench.py micro
    python bench/bench.py loaded
    python bench/bench.py macro
    python bench/bench.py failover
    python bench/bench.py e2e --upstream 192.168.212.21:17090

只依赖 Python 标准库；macro 需要系统自带 ab，e2e 需要 curl（两者缺失会自动跳过）。
"""

import argparse
import json
import math
import os
import platform
import re
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
TARGET = Path(__file__).resolve().parent / "target.py"
RESULT_ROOT = REPO / "bench" / "results"
RUST_BIN = REPO / "rust" / "target" / "release" / "pswitch"

# 由 main() 根据 --impl / --binary 设置；None 表示用 `python -m pswitch`。
IMPL = "python"
BIN_PATH = None

PROXY_ENV_KEYS = (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
)


# --------------------------------------------------------------------------
# 计时与统计
# --------------------------------------------------------------------------

def pct(sorted_xs, p):
    if not sorted_xs:
        return None
    if len(sorted_xs) == 1:
        return float(sorted_xs[0])
    k = (len(sorted_xs) - 1) * p / 100.0
    lo = int(math.floor(k))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return float(sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo))


def summarize(xs):
    """输入微秒/毫秒样本，输出常用分位数。"""
    xs = sorted(float(x) for x in xs)
    if not xs:
        return {"n": 0, "mean": None, "stdev": None,
                "p50": None, "p90": None, "p99": None, "max": None}
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs),
        "stdev": statistics.stdev(xs) if len(xs) > 1 else 0.0,
        "p50": pct(xs, 50),
        "p90": pct(xs, 90),
        "p99": pct(xs, 99),
        "max": xs[-1],
    }


def collect_metrics(prefix, obj, bucket):
    """把嵌套的 summarize() 结果拍平成 {key: [每轮数值]}。"""
    stats = ("p50", "p90", "p99", "mean", "max")
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            if all(s in v for s in ("p50", "p99")):
                for stat in stats:
                    if isinstance(v.get(stat), (int, float)):
                        bucket.setdefault(f"{key}.{stat}", []).append(float(v[stat]))
            else:
                collect_metrics(key, v, bucket)


def aggregate_rounds(rounds):
    bucket = {}
    for r in rounds:
        collect_metrics("", r, bucket)
    return {k: statistics.median(v) for k, v in bucket.items() if v}


def fmt(value, digits=1):
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


# --------------------------------------------------------------------------
# socket 小工具
# --------------------------------------------------------------------------

def recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:])
        if not k:
            raise ConnectionError("对端关闭")
        got += k
    return bytes(buf)


def recv_until(sock, marker, limit=65536):
    buf = b""
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("读到 EOF 也没等到响应头")
        buf += chunk
        if len(buf) > limit:
            raise ConnectionError("响应头过长")
    return buf


def open_conn(port, nodelay=True, rst_on_close=False, timeout=5.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    if nodelay:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if rst_on_close:
        # 高频建连时用 RST 关闭，避免 TIME_WAIT 把本地端口耗光。
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    return s


def do_connect_tunnel(sock, target):
    host, port = target
    sock.sendall(
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n\r\n".encode())
    head = recv_until(sock, b"\r\n\r\n")
    if not head.startswith(b"HTTP/1.1 200"):
        raise RuntimeError(f"CONNECT 失败: {head[:80]!r}")


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port(port, timeout, name):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{name} 未在 {timeout}s 内监听 127.0.0.1:{port}")


def closed_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------
# 进程与 pswitch 实例
# --------------------------------------------------------------------------

def clean_env():
    env = os.environ.copy()
    for k in PROXY_ENV_KEYS:
        env.pop(k, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = env["NO_PROXY"]
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def cpu_time(pid):
    """读进程累计 CPU 秒数（macOS/Linux 的 ps 都支持 -o time=）。"""
    try:
        out = subprocess.run(
            ["ps", "-o", "time=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    days = 0
    if "-" in out:
        d, out = out.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return None
    parts = out.split(":")
    try:
        seconds = float(parts[-1])
        if len(parts) >= 2:
            seconds += int(parts[-2]) * 60
        if len(parts) >= 3:
            seconds += int(parts[-3]) * 3600
    except ValueError:
        return None
    return days * 86400 + seconds


class Proc:
    def __init__(self, name, cmd, env=None, log_path=None):
        self.name = name
        self.cmd = cmd
        self.env = env
        self.log_path = Path(log_path) if log_path else None
        self.p = None
        self._logf = None

    def start(self, wait=False):
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._logf = open(self.log_path, "ab")
        else:
            self._logf = subprocess.DEVNULL
        self.p = subprocess.Popen(
            self.cmd, env=self.env, cwd=str(REPO),
            stdin=subprocess.DEVNULL, stdout=self._logf,
            stderr=subprocess.STDOUT, start_new_session=True)
        if wait:
            wait_port(self.port, 20, self.name)
        return self

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        if self._logf and self._logf is not subprocess.DEVNULL:
            try:
                self._logf.close()
            except Exception:
                pass
            self._logf = None


class Instance(Proc):
    """一个独立的 pswitch 守护进程。"""

    def __init__(self, root, name, port, mode, upstreams=None, active=None,
                 connect_timeout=3.0, failover=True, probe_url=None):
        self.port = port
        self.mode = mode
        self.home = root / "env" / name
        self.home.mkdir(parents=True, exist_ok=True)
        cfg = {
            "listen": f"127.0.0.1:{port}",
            "mode": mode,
            "active": active,
            "health_check_interval": 3600,   # 测试期间不做周期探活
            "connect_timeout": connect_timeout,
            "failover": failover,
            "probe_url": probe_url or f"http://127.0.0.1:{port}/",
            "probe_timeout": 0.5,
            "upstreams": [
                {"name": n, "addr": a, "enabled": True}
                for n, a in (upstreams or [])
            ],
        }
        (self.home / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        child_env = clean_env()
        # 硬隔离：子进程只能读这次运行自己的 PSWITCH_HOME。
        child_env["PSWITCH_HOME"] = str(self.home)
        cmd = ([BIN_PATH, "serve"] if BIN_PATH
               else [sys.executable, "-m", "pswitch", "serve"])
        super().__init__(
            name,
            cmd,
            env=child_env,
            log_path=self.home / "stdout.log")

    def start(self, wait=True):
        return super().start(wait=wait)


class Env:
    def __init__(self, args):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.root = Path(args.out).expanduser() if args.out else RESULT_ROOT / stamp
        self.root.mkdir(parents=True, exist_ok=True)
        off = args.offset
        self.raw_port = 18080 + off
        self.http_port = 18081 + off
        self.forward_port = 17100 + off
        self.direct_port = 17101 + off
        self.chain_port = 17102 + off
        self.http_forward_port = 17103 + off
        self.e2e_port = 17104 + off
        self.failover_port = 17105 + off
        self.throughput_port = 17106 + off
        self.iperf_port = 18082 + off
        self.procs = []
        self.instances = {}
        self.targets = {}
        self.core_started = False
        self.http_started = False
        self.throughput_started = False

    # -- 生命周期 --------------------------------------------------------
    def _register(self, proc):
        self.procs.append(proc)
        return proc

    def ensure_target(self, name, port, mode):
        """同一个目标端只启动一次；重复调用直接复用。"""
        proc = self.targets.get(name)
        if proc and proc.p and proc.p.poll() is None:
            return proc
        proc = Proc(
            name,
            [sys.executable, str(TARGET), "--port", str(port), "--mode", mode],
            env=clean_env(),
            log_path=self.root / "env" / name / "stdout.log")
        self._register(proc).start()
        wait_port(port, 10, name)
        self.targets[name] = proc
        return proc

    def start_instance(self, name, port, mode, **kw):
        inst = Instance(self.root, name, port, mode, **kw)
        self._register(inst).start(wait=True)
        self.instances[name] = inst
        return inst

    def start_core(self):
        if self.core_started:
            return
        for port, name in ((self.raw_port, "raw target"),
                           (self.forward_port, "forward"),
                           (self.direct_port, "direct"),
                           (self.chain_port, "chain")):
            if not port_free(port):
                raise SystemExit(
                    f"端口 {port} 被占用（{name}）。换一组端口: --offset 100")
        self.ensure_target("raw-target", self.raw_port, "raw")
        self.start_instance("direct", self.direct_port, "direct")
        self.start_instance(
            "forward", self.forward_port, "manual",
            upstreams=[("raw", f"127.0.0.1:{self.raw_port}")],
            active="raw",
            probe_url=f"http://127.0.0.1:{self.raw_port}/")
        # 转发模式的上游必须自己会讲代理协议，所以再套一个 direct 实例。
        self.start_instance(
            "chain", self.chain_port, "manual",
            upstreams=[("direct", f"127.0.0.1:{self.direct_port}")],
            active="direct",
            probe_url=f"http://127.0.0.1:{self.raw_port}/")
        self.core_started = True

    def start_http(self):
        if self.http_started:
            return
        for port in (self.http_port, self.http_forward_port):
            if not port_free(port):
                raise SystemExit(f"端口 {port} 被占用，换一组端口: --offset 100")
        self.ensure_target("http-target", self.http_port, "http")
        self.start_instance(
            "http-forward", self.http_forward_port, "manual",
            upstreams=[("http", f"127.0.0.1:{self.http_port}")],
            active="http",
            probe_url=f"http://127.0.0.1:{self.http_port}/")
        self.http_started = True

    def start_throughput(self, generator):
        if self.throughput_started:
            return
        if not port_free(self.throughput_port):
            raise SystemExit(
                f"端口 {self.throughput_port} 被占用，换一组端口: --offset 100")
        if generator == "iperf3":
            iperf = shutil.which("iperf3")
            if not iperf:
                raise RuntimeError("找不到 iperf3")
            if not port_free(self.iperf_port):
                raise SystemExit(
                    f"端口 {self.iperf_port} 被占用，换一组端口: --offset 100")
            server = Proc(
                "iperf-server",
                [iperf, "-s", "-p", str(self.iperf_port)],
                env=clean_env(),
                log_path=self.root / "env" / "iperf-server" / "stdout.log")
            self._register(server).start()
            time.sleep(0.5)
            if server.p.poll() is not None:
                raise RuntimeError("iperf3 server 启动失败")
            upstream = f"127.0.0.1:{self.iperf_port}"
            probe_url = f"http://127.0.0.1:{self.iperf_port}/"
        else:
            self.ensure_target("raw-target", self.raw_port, "raw")
            upstream = f"127.0.0.1:{self.raw_port}"
            probe_url = f"http://127.0.0.1:{self.raw_port}/"
        self.start_instance(
            "throughput", self.throughput_port, "manual",
            upstreams=[("sink", upstream)], active="sink",
            probe_url=probe_url)
        self.throughput_started = True

    def cleanup(self):
        for proc in reversed(self.procs):
            try:
                proc.stop()
            except Exception as e:
                print(f"[cleanup] {proc.name}: {e}", file=sys.stderr)

    def save(self, name, data):
        (self.root / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 微基准：稳态 RTT + 新建连接延迟
# --------------------------------------------------------------------------

def echo_session(port, size, samples, warmup, connect_target=None):
    """一条长连接上连续打 N 次小包，测稳态 RTT（微秒）。"""
    s = open_conn(port)
    handshake = []
    rtts = []
    try:
        if connect_target:
            t0 = time.perf_counter_ns()
            do_connect_tunnel(s, connect_target)
            handshake.append((time.perf_counter_ns() - t0) / 1000.0)
        msg = b"E" + b"x" * size
        for i in range(warmup + samples):
            t0 = time.perf_counter_ns()
            s.sendall(msg)
            recv_exact(s, size)
            dt = (time.perf_counter_ns() - t0) / 1000.0
            if i >= warmup:
                rtts.append(dt)
    finally:
        s.close()
    return {
        "rtt": summarize(rtts),
        "handshake": summarize(handshake),
    }


def fresh_session(port, size, samples, connect_target=None):
    """每次新建连接，拆出 connect / CONNECT 握手 / 首个 payload RTT。"""
    msg = b"E" + b"x" * size
    conn, setup, payload, total = [], [], [], []
    for _ in range(samples):
        t0 = time.perf_counter_ns()
        s = open_conn(port, rst_on_close=True)
        t1 = time.perf_counter_ns()
        base = t1
        try:
            if connect_target:
                t2 = time.perf_counter_ns()
                do_connect_tunnel(s, connect_target)
                setup.append((time.perf_counter_ns() - t2) / 1000.0)
                base = time.perf_counter_ns()
            s.sendall(msg)
            recv_exact(s, size)
            t3 = time.perf_counter_ns()
        finally:
            s.close()
        conn.append((t1 - t0) / 1000.0)
        payload.append((t3 - base) / 1000.0)
        total.append((t3 - t0) / 1000.0)
    return {
        "connect": summarize(conn),
        "setup": summarize(setup),
        "payload": summarize(payload),
        "total": summarize(total),
    }


def run_micro(env, args):
    topologies = [
        ("T0_baseline", env.raw_port, None),
        ("T1_forward", env.forward_port, None),
        ("T2_forward_chain", env.chain_port, ("127.0.0.1", env.raw_port)),
        ("T3_direct", env.direct_port, ("127.0.0.1", env.raw_port)),
    ]
    rounds = []
    for r in range(args.rounds):
        # 奇偶轮颠倒顺序，抵消先后顺序带来的漂移
        order = topologies if r % 2 == 0 else list(reversed(topologies))
        round_res = {}
        for name, port, tunnel in order:
            for size in args.payloads:
                key = f"{name}|{size}"
                steady = echo_session(port, size, args.samples,
                                      args.warmup, tunnel)
                fresh = fresh_session(port, size, args.fresh_samples, tunnel)
                round_res[key] = {"steady": steady, "fresh": fresh}
        rounds.append(round_res)
        print(f"[micro] round {r + 1}/{args.rounds} 完成")
    return {
        "payloads": args.payloads,
        "rounds_detail": rounds,
        "aggregate": aggregate_rounds(rounds),
    }


# --------------------------------------------------------------------------
# 加载下的延迟：背景灌包 + 小包探测
# --------------------------------------------------------------------------

def bulk_worker(port, stop, counter):
    try:
        s = open_conn(port, timeout=10)
        s.sendall(b"S")
        chunk = b"\x00" * 65536
        while not stop.is_set():
            s.sendall(chunk)
            counter[0] += len(chunk)
        s.close()
    except OSError:
        pass


def probe_worker(port, stop, samples, interval):
    try:
        s = open_conn(port)
        s.sendall(b"E")
        payload = b"12345678"
        while not stop.is_set():
            t0 = time.perf_counter_ns()
            s.sendall(payload)
            recv_exact(s, len(payload))
            dt = (time.perf_counter_ns() - t0) / 1000.0
            samples.append((time.monotonic(), dt))
            elapsed = dt / 1e6
            if elapsed < interval:
                time.sleep(interval - elapsed)
        s.close()
    except OSError:
        pass


def concurrent_worker(port, count, barrier, samples, errors):
    """K 条连接同时做顺序 echo，用来观察并发下的排队尾延迟。"""
    try:
        s = open_conn(port, timeout=10)
        s.sendall(b"E")
        for _ in range(20):                # 每条连接先预热
            s.sendall(b"12345678")
            recv_exact(s, 8)
        barrier.wait(timeout=10)
        for _ in range(count):
            t0 = time.perf_counter_ns()
            s.sendall(b"12345678")
            recv_exact(s, 8)
            samples.append((time.perf_counter_ns() - t0) / 1000.0)
        s.close()
    except (OSError, RuntimeError) as e:
        errors.append(str(e))


def run_concurrent(env, args):
    results = []
    for k in args.concurrent:
        barrier = threading.Barrier(k)
        samples, errors = [], []
        threads = [
            threading.Thread(
                target=concurrent_worker,
                args=(env.forward_port, args.concurrent_requests,
                      barrier, samples, errors),
                daemon=True)
            for _ in range(k)
        ]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0
        summary = summarize(samples)
        results.append({
            "concurrency": k,
            "requests": k * args.concurrent_requests,
            "duration_s": round(elapsed, 3),
            "req_per_sec": (k * args.concurrent_requests / elapsed
                            if elapsed > 0 else None),
            "rtt": summary,
            "errors": errors[:5],
        })
        print(f"[loaded] concurrent={k} "
              f"{fmt(results[-1]['req_per_sec'], 0)} req/s "
              f"p50={fmt(summary['p50'])}us p99={fmt(summary['p99'])}us")
    return results


def run_loaded(env, args):
    pid = env.instances["forward"].p.pid
    # 全局预热：macOS 的 CPU 频率/缓存需要一点时间才稳定，否则第一档会虚高。
    warm_stop = threading.Event()
    warm = []
    warm_thread = threading.Thread(
        target=probe_worker,
        args=(env.forward_port, warm_stop, warm, args.probe_interval),
        daemon=True)
    warm_thread.start()
    time.sleep(1.5)
    warm_stop.set()
    warm_thread.join(timeout=3)

    results = []
    for streams in args.streams:
        stop = threading.Event()
        counters = [[0] for _ in range(streams)]
        probes = []
        bulk_threads = [
            threading.Thread(target=bulk_worker,
                             args=(env.forward_port, stop, counters[i]),
                             daemon=True)
            for i in range(streams)
        ]
        probe_thread = threading.Thread(
            target=probe_worker,
            args=(env.forward_port, stop, probes, args.probe_interval),
            daemon=True)

        cpu0 = cpu_time(pid)
        t0 = time.monotonic()
        for t in bulk_threads:
            t.start()
        probe_thread.start()
        time.sleep(args.duration)
        stop.set()
        probe_thread.join(timeout=5)
        for t in bulk_threads:
            t.join(timeout=5)
        elapsed = time.monotonic() - t0
        cpu1 = cpu_time(pid)

        total_bytes = sum(c[0] for c in counters)
        gbps = total_bytes * 8 / elapsed / 1e9 if elapsed > 0 else None
        cut = t0 + 0.5          # 丢掉前 0.5s 的预热样本
        rtts = [dt for ts, dt in probes if ts >= cut]
        cpu_pct = None
        if cpu0 is not None and cpu1 is not None and elapsed > 0:
            cpu_pct = (cpu1 - cpu0) / elapsed * 100.0

        results.append({
            "streams": streams,
            "duration_s": round(elapsed, 3),
            "bulk_gbps": gbps,
            "probe": summarize(rtts),
            "pswitch_cpu_pct": cpu_pct,
        })
        print(f"[loaded] streams={streams} "
              f"bulk={fmt(gbps, 2)} Gbps "
              f"probe p50={fmt(results[-1]['probe']['p50'])}us "
              f"p99={fmt(results[-1]['probe']['p99'])}us")
    return {
        "levels": results,
        "concurrency": run_concurrent(env, args),
    }


# --------------------------------------------------------------------------
# 转发吞吐：iperf3（首选）与内置 Python 生成器（回退）
# --------------------------------------------------------------------------

def download_worker(port, stop, counter):
    try:
        s = open_conn(port, timeout=10)
        s.sendall(b"D")
        while not stop.is_set():
            data = s.recv(65536)
            if not data:
                break
            counter[0] += len(data)
        s.close()
    except OSError:
        pass


def _iperf_throughput(data, direction):
    end = data.get("end") or {}
    recv = (end.get("sum_received") or {}).get("bits_per_second")
    rev = (end.get("sum_received_bidir_reverse") or {}).get("bits_per_second")
    if direction == "bidir":
        if recv is None and rev is None:
            return None
        return ((recv or 0) + (rev or 0)) / 1e9
    return recv / 1e9 if recv is not None else None


def iperf_run(binary, port, direction, streams, args):
    cmd = [binary, "-c", "127.0.0.1", "-p", str(port),
           "-t", str(int(args.throughput_duration)),
           "-O", str(int(args.throughput_omit)),
           "-P", str(streams), "-w", args.throughput_window, "-J"]
    if direction == "recv":
        cmd.append("-R")
    elif direction == "bidir":
        cmd.append("--bidir")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=args.throughput_duration + 60)
    except subprocess.TimeoutExpired:
        return {"error": "iperf3 超时"}
    if p.returncode != 0:
        return {"error": (p.stderr or p.stdout).strip()[:300]}
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"error": "iperf3 输出不是 JSON", "raw": p.stdout[-500:]}
    end = data.get("end") or {}
    sent = end.get("sum_sent") or {}
    recv = end.get("sum_received") or {}
    rev = end.get("sum_received_bidir_reverse") or {}
    return {
        "gbps": _iperf_throughput(data, direction),
        "recv_gbps": (recv.get("bits_per_second") or 0) / 1e9 or None,
        "rev_gbps": ((rev.get("bits_per_second") or 0) / 1e9
                     if direction == "bidir" else None),
        "retransmits": sent.get("retransmits", recv.get("retransmits")),
        "cpu_host_pct": (end.get("cpu_utilization_percent") or {}).get("host_total"),
        "cpu_remote_pct": (end.get("cpu_utilization_percent") or {}).get("remote_total"),
    }


def python_arm(port, direction, streams, duration):
    stop = threading.Event()
    parts = streams * (2 if direction == "bidir" else 1)
    counters = [[0] for _ in range(parts)]
    threads = []
    idx = 0
    if direction in ("send", "bidir"):
        for _ in range(streams):
            threads.append(threading.Thread(
                target=bulk_worker, args=(port, stop, counters[idx]),
                daemon=True))
            idx += 1
    if direction in ("recv", "bidir"):
        for _ in range(streams):
            threads.append(threading.Thread(
                target=download_worker, args=(port, stop, counters[idx]),
                daemon=True))
            idx += 1
    t0 = time.monotonic()
    for t in threads:
        t.start()
    time.sleep(duration)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - t0
    total = sum(c[0] for c in counters)
    return {"gbps": total * 8 / elapsed / 1e9 if elapsed > 0 else None}


def run_throughput(env, args):
    iperf = shutil.which("iperf3")
    generator = "iperf3" if (
        args.throughput_generator in ("auto", "iperf3") and iperf) else "python"
    try:
        env.start_throughput(generator)
    except Exception as e:
        if generator == "iperf3":
            print(f"[throughput] iperf3 不可用，回退 Python: {e}")
            generator = "python"
            env.start_throughput(generator)
        else:
            raise

    pid = env.instances["throughput"].p.pid
    baseline_port = (env.iperf_port if generator == "iperf3"
                     else env.raw_port)
    cases = []
    for direction in args.throughput_directions:
        for streams in args.throughput_streams:
            rounds = []
            for r in range(args.throughput_rounds):
                order = (("baseline", "pswitch") if r % 2 == 0
                         else ("pswitch", "baseline"))
                detail = {}
                for arm in order:
                    port = (baseline_port if arm == "baseline"
                            else env.throughput_port)
                    cpu0 = cpu_time(pid) if arm == "pswitch" else None
                    t0 = time.monotonic()
                    if generator == "iperf3":
                        res = iperf_run(iperf, port, direction, streams, args)
                    else:
                        res = python_arm(port, direction, streams,
                                         args.throughput_duration)
                    elapsed = time.monotonic() - t0
                    cpu1 = cpu_time(pid) if arm == "pswitch" else None
                    if (arm == "pswitch" and cpu0 is not None
                            and cpu1 is not None and elapsed > 0):
                        res["cpu_pct"] = (cpu1 - cpu0) / elapsed * 100.0
                    detail[arm] = res
                rounds.append(detail)

            def med(arm, field):
                vals = [x[arm].get(field) for x in rounds
                        if x.get(arm) and isinstance(x[arm].get(field), (int, float))]
                return statistics.median(vals) if vals else None

            base_gbps = med("baseline", "gbps")
            ps_gbps = med("pswitch", "gbps")
            ps_cpu = med("pswitch", "cpu_pct")
            case = {
                "direction": direction,
                "streams": streams,
                "baseline": {"gbps": base_gbps},
                "pswitch": {
                    "gbps": ps_gbps,
                    "cpu_pct": ps_cpu,
                    "gbps_per_core": (ps_gbps / (ps_cpu / 100.0)
                                      if ps_gbps and ps_cpu else None),
                },
                "retention": (ps_gbps / base_gbps
                              if ps_gbps is not None and base_gbps else None),
                "rounds": rounds,
            }
            cases.append(case)
            retention_pct = (case["retention"] * 100
                             if case["retention"] is not None else None)
            print(f"[throughput] {direction} P={streams} "
                  f"baseline={fmt(base_gbps, 2)} Gbps "
                  f"pswitch={fmt(ps_gbps, 2)} Gbps "
                  f"retention={fmt(retention_pct, 0)}% "
                  f"cpu={fmt(ps_cpu, 0)}%")
    return {
        "plan_version": "v1",
        "generator": generator,
        "params": {
            "duration_s": args.throughput_duration,
            "omit_s": args.throughput_omit,
            "rounds": args.throughput_rounds,
            "window": args.throughput_window,
        },
        "cases": cases,
    }


# --------------------------------------------------------------------------
# 宏基准：ab（keep-alive）
# --------------------------------------------------------------------------

def ab_run(binary, url, requests, concurrency):
    cmd = [binary, "-q", "-k", "-s", "10",
           "-n", str(requests), "-c", str(concurrency), url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=requests / 1000 + 120)
    except subprocess.TimeoutExpired:
        return {"error": "ab 超时", "cmd": " ".join(cmd)}
    out = p.stdout + p.stderr
    rps = re.search(r"Requests per second:\s+([\d.]+)", out)
    failed = re.search(r"Failed requests:\s+(\d+)", out)
    percentiles = {
        int(a): float(b)
        for a, b in re.findall(r"^\s*(\d+)%\s+([\d.]+)\s*$", out, re.MULTILINE)
    }
    result = {
        "requests_per_sec": float(rps.group(1)) if rps else None,
        "failed_requests": int(failed.group(1)) if failed else None,
        "p50_ms": percentiles.get(50),
        "p90_ms": percentiles.get(90),
        "p99_ms": percentiles.get(99),
        "raw": out[-2000:],
    }
    if p.returncode != 0:
        result["error"] = f"ab 退出码 {p.returncode}"
    return result


def run_macro(env, args):
    ab = shutil.which("ab")
    if not ab:
        return {"skipped": "系统没有 ab，跳过宏基准"}
    env.start_http()
    cases = []
    for c in args.concurrency:
        n = max(args.requests, c)
        baseline = ab_run(ab, f"http://127.0.0.1:{env.http_port}/", n, c)
        proxied = ab_run(ab, f"http://127.0.0.1:{env.http_forward_port}/", n, c)
        cases.append({
            "concurrency": c,
            "requests": n,
            "baseline": baseline,
            "pswitch": proxied,
        })
        print(f"[macro] c={c} "
              f"baseline={fmt(baseline.get('requests_per_sec'))} req/s "
              f"pswitch={fmt(proxied.get('requests_per_sec'))} req/s")
    return {"cases": cases}


# --------------------------------------------------------------------------
# 故障切换：上游拒绝 / 黑洞 / 全部不可用
# --------------------------------------------------------------------------

def time_to_eof(port, timeout):
    s = open_conn(port, timeout=timeout)
    try:
        t0 = time.perf_counter_ns()
        s.sendall(b"E" + b"x")
        s.settimeout(timeout)
        try:
            data = s.recv(1)
        except socket.timeout:
            return None
        except ConnectionResetError:
            # RST 同样属于"快速失败"，不是挂住
            return (time.perf_counter_ns() - t0) / 1000.0
        return (time.perf_counter_ns() - t0) / 1000.0 if data == b"" else None
    finally:
        s.close()


def run_failover(env, args):
    good = f"127.0.0.1:{env.raw_port}"
    env.ensure_target("raw-target", env.raw_port, "raw")
    cases = []

    def measure(name, upstreams, active, timeout):
        inst = env.start_instance(
            f"failover-{name}", env.failover_port, "manual",
            upstreams=upstreams, active=active,
            connect_timeout=timeout,
            probe_url=f"http://127.0.0.1:{env.raw_port}/")
        try:
            fresh = fresh_session(env.failover_port, 1, args.failover_samples)
            cases.append({"case": name, "connect_timeout": timeout, **fresh})
        except Exception as e:
            cases.append({"case": name, "connect_timeout": timeout,
                          "error": str(e)})
        finally:
            inst.stop()

    bad_refused = f"127.0.0.1:{closed_port()}"
    measure("refused_failover",
            [("bad", bad_refused), ("good", good)], "bad", 1.0)

    measure("blackhole_failover",
            [("bad", args.blackhole), ("good", good)], "bad",
            args.connect_timeout)

    # 全部不可用：客户端应该很快看到 EOF，而不是一直挂着。
    inst = env.start_instance(
        "failover-all-down", env.failover_port, "manual",
        upstreams=[("bad", bad_refused)], active="bad",
        connect_timeout=args.connect_timeout, failover=False,
        probe_url=f"http://127.0.0.1:{env.raw_port}/")
    try:
        eof_us = time_to_eof(env.failover_port, args.connect_timeout + 2)
        cases.append({
            "case": "all_down",
            "connect_timeout": args.connect_timeout,
            "eof_us": eof_us,
            "total": summarize([eof_us]) if eof_us is not None else None,
        })
    finally:
        inst.stop()

    print("[failover] " + ", ".join(c["case"] for c in cases))
    return {"cases": cases}


# --------------------------------------------------------------------------
# 端到端：真实上游的配对差值
# --------------------------------------------------------------------------

CURL_FORMAT = ("%{time_connect} %{time_appconnect} "
               "%{time_starttransfer} %{time_total} %{http_code}")


def curl_once(curl, proxy, url, timeout):
    cmd = [curl, "-sS", "-o", "/dev/null", "--http1.1",
           "--max-time", str(timeout), "-x", proxy,
           "-w", CURL_FORMAT, url]
    env = clean_env()
    env["NO_PROXY"] = ""          # 让 -x 生效，不被 no_proxy 覆盖
    env["no_proxy"] = ""
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return {"error": (p.stderr or "").strip()[:200]}
    try:
        connect, appconnect, ttfb, total, code = p.stdout.split()
        return {
            "connect_ms": float(connect) * 1000,
            "tls_ms": float(appconnect) * 1000,
            "ttfb_ms": float(ttfb) * 1000,
            "total_ms": float(total) * 1000,
            "http_code": int(code),
        }
    except ValueError:
        return {"error": f"无法解析 curl 输出: {p.stdout!r}"}


def run_e2e(env, args):
    curl = shutil.which("curl")
    if not curl:
        return {"skipped": "系统没有 curl"}
    if not args.upstream:
        return {"skipped": "未提供 --upstream"}
    inst = env.start_instance(
        "e2e", env.e2e_port, "manual",
        upstreams=[("upstream", args.upstream)], active="upstream",
        connect_timeout=args.connect_timeout, probe_url=args.url)
    direct_proxy = f"http://{args.upstream}"
    local_proxy = f"http://127.0.0.1:{env.e2e_port}"
    pairs = []
    try:
        for i in range(args.e2e_samples):
            arms = (("upstream", direct_proxy), ("pswitch", local_proxy))
            if i % 2:
                arms = tuple(reversed(arms))
            result = {}
            for name, proxy in arms:
                result[name] = curl_once(curl, proxy, args.url, args.timeout)
            pairs.append(result)
    finally:
        inst.stop()

    deltas = {"connect_ms": [], "tls_ms": [], "ttfb_ms": [], "total_ms": []}
    for pair in pairs:
        up, ps = pair.get("upstream", {}), pair.get("pswitch", {})
        if "error" in up or "error" in ps:
            continue
        for field in deltas:
            deltas[field].append(ps[field] - up[field])
    return {
        "url": args.url,
        "upstream": args.upstream,
        "samples": args.e2e_samples,
        "pairs": pairs,
        "delta_ms": {k: summarize(v) for k, v in deltas.items()},
    }


# --------------------------------------------------------------------------
# 结果汇总
# --------------------------------------------------------------------------

MICRO_TOPO_DESC = {
    "T0_baseline": "client -> target",
    "T1_forward": "client -> pswitch(forward) -> target",
    "T2_forward_chain": "client -> pswitch(forward) -> pswitch(direct) -> target",
    "T3_direct": "client -> pswitch(direct) -> target",
}


def build_summary(env, args, meta, results):
    L = []
    L.append("# pswitch 延迟基准")
    L.append("")
    L.append(f"- 方案版本: {meta['plan_version']}")
    L.append(f"- 时间: {meta['started_at']}")
    L.append(f"- 主机: {meta['host']} / {meta['platform']} / Python {meta['python']}")
    L.append(f"- CPU 核心: {meta['cpu_count']}")
    L.append(f"- 实现: {meta.get('impl', 'python')}"
             + (f" (`{meta['binary']}`)" if meta.get("binary") else ""))
    L.append(f"- pswitch: {meta['pswitch_version']} (git {meta['git_commit']})")
    L.append(f"- 结果目录: `{env.root}`")
    L.append("")
    L.append("所有 pswitch 实例都使用临时 `PSWITCH_HOME`，"
             "监听 171xx 端口，未触碰生产实例 127.0.0.1:17000。")
    L.append("")

    verdicts = []

    micro = results.get("micro")
    if micro:
        agg = micro["aggregate"]
        L.append("## 微基准（单位 µs）")
        L.append("")
        L.append("| 拓扑 | payload | steady p50 | steady p99 | fresh total p50 "
                 "| fresh total p99 |")
        L.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for name in MICRO_TOPO_DESC:
            for size in micro["payloads"]:
                key = f"{name}|{size}"

                def g(stat, key=key):
                    return agg.get(f"{key}.steady.rtt.{stat}")

                def f(stat, key=key):
                    return agg.get(f"{key}.fresh.total.{stat}")

                L.append(
                    f"| {name} | {size} B | {fmt(g('p50'))} | {fmt(g('p99'))} "
                    f"| {fmt(f('p50'))} | {fmt(f('p99'))} |")
        L.append("")
        L.append("### pswitch 附加开销（相对 T0）")
        L.append("")
        L.append("| 对比 | steady p50 | steady p99 | fresh p50 | fresh p99 |")
        L.append("| --- | ---: | ---: | ---: | ---: |")
        for name in ("T1_forward", "T2_forward_chain", "T3_direct"):
            for size in micro["payloads"]:
                key = f"{name}|{size}"
                base = f"T0_baseline|{size}"

                def d(section, stat, key=key, base=base):
                    sub = "rtt" if section == "steady" else "total"
                    a = agg.get(f"{key}.{section}.{sub}.{stat}")
                    b = agg.get(f"{base}.{section}.{sub}.{stat}")
                    return None if a is None or b is None else a - b

                L.append(
                    f"| {name} / {size} B "
                    f"| {fmt(d('steady', 'p50'))} | {fmt(d('steady', 'p99'))} "
                    f"| {fmt(d('fresh', 'p50'))} | {fmt(d('fresh', 'p99'))} |")
        L.append("")

        p50 = agg.get("T1_forward|1.steady.rtt.p50")
        p99 = agg.get("T1_forward|1.steady.rtt.p99")
        fresh_p50 = agg.get("T1_forward|1.fresh.total.p50")
        if p50 is not None:
            verdicts.append(("T1 稳态 1B p50 <= 500 µs", p50 <= 500, f"{fmt(p50)} µs"))
        if p99 is not None:
            verdicts.append(("T1 稳态 1B p99 <= 2000 µs", p99 <= 2000, f"{fmt(p99)} µs"))
        if fresh_p50 is not None:
            verdicts.append(("T1 新建连接首包 p50 <= 2000 µs",
                             fresh_p50 <= 2000, f"{fmt(fresh_p50)} µs"))

    loaded = results.get("loaded")
    if loaded:
        L.append("## 加载下的延迟（背景灌包 + 10ms 小包探测）")
        L.append("")
        L.append("| 背景流 | 背景吞吐 Gbps | 探测 p50 µs | 探测 p90 µs "
                 "| 探测 p99 µs | pswitch CPU |")
        L.append("| ---: | ---: | ---: | ---: | ---: | ---: |")
        for level in loaded["levels"]:
            p = level["probe"]
            cpu = level.get("pswitch_cpu_pct")
            L.append(
                f"| {level['streams']} | {fmt(level.get('bulk_gbps'), 2)} "
                f"| {fmt(p['p50'])} | {fmt(p['p90'])} | {fmt(p['p99'])} "
                f"| {fmt(cpu, 0)}% |")
        L.append("")
        L.append("> 探测流每 10 ms 才发一次；0 背景流时测到的主要是进程从空闲被唤醒的"
                 "开销（同机直连目标端也能复现，约数百 µs）。持续请求下的真实延迟"
                 "看下一张表里并发=1 的结果。")
        L.append("")
        loaded_p99 = [lv["probe"]["p99"] for lv in loaded["levels"]
                      if lv["streams"] > 0 and lv["probe"]["p99"] is not None]
        if loaded_p99:
            worst = max(loaded_p99)
            verdicts.append(("加载下探测 p99 <= 5000 µs", worst <= 5000,
                             f"{fmt(worst)} µs"))

        concurrent = loaded.get("concurrency") or []
        if concurrent:
            L.append("## 并发下的稳态 RTT（同一 forward 实例）")
            L.append("")
            L.append("| 并发 | 请求数 | req/s | p50 µs | p90 µs | p99 µs "
                     "| max µs | 错误 |")
            L.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
            for level in concurrent:
                r = level["rtt"]
                L.append(
                    f"| {level['concurrency']} | {level['requests']} "
                    f"| {fmt(level['req_per_sec'], 0)} "
                    f"| {fmt(r['p50'])} | {fmt(r['p90'])} "
                    f"| {fmt(r['p99'])} | {fmt(r['max'])} "
                    f"| {len(level['errors'])} |")
            L.append("")

    thr = results.get("throughput")
    if thr:
        L.append("## 转发吞吐")
        L.append("")
        L.append(f"- 生成器: `{thr['generator']}`")
        if thr["generator"] == "python":
            L.append("- Python 生成器与 pswitch 同机争 CPU，绝对值偏低；"
                     "只看同一行里 baseline 与 pswitch 的相对关系。")
        L.append("")
        L.append("| 方向 | 流数 | 基线 Gbps | pswitch Gbps | 保持率 "
                 "| pswitch CPU | Gbps/核 |")
        L.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for case in thr["cases"]:
            b = case["baseline"].get("gbps")
            p = case["pswitch"].get("gbps")
            r = case["retention"]
            cpu = case["pswitch"].get("cpu_pct")
            per = case["pswitch"].get("gbps_per_core")
            L.append(
                f"| {case['direction']} | {case['streams']} "
                f"| {fmt(b, 2)} | {fmt(p, 2)} "
                f"| {fmt(r * 100 if r is not None else None, 0)}% "
                f"| {fmt(cpu, 0)}% | {fmt(per, 1)} |")
        L.append("")
        L.append("> 基线是内核 loopback（单流可达 100+ Gbps），pswitch 是单核用户态代理"
                 "（约 30–40 Gbps）；单流保持率只作参考，不作为判定项。"
                 "四流基线反而下降是 iperf3 自身的多流开销，"
                 "不能直接和单流基线比较。")
        L.append("")
        if thr["generator"] == "iperf3":
            def send_case(n):
                return next((c for c in thr["cases"]
                             if c["direction"] == "send"
                             and c["streams"] == n), None)

            one, four = send_case(1), send_case(4)
            if one and one["pswitch"].get("gbps") is not None:
                gbps = one["pswitch"]["gbps"]
                verdicts.append(("单流 send >= 10 Gbps", gbps >= 10,
                                 f"{fmt(gbps, 2)} Gbps"))
            if four and four["pswitch"].get("gbps") is not None:
                gbps = four["pswitch"]["gbps"]
                verdicts.append(("四流 send >= 20 Gbps", gbps >= 20,
                                 f"{fmt(gbps, 2)} Gbps"))
        L.append("")

    macro = results.get("macro")
    if macro and "cases" in macro:
        L.append("## 宏基准（ab -k，keep-alive）")
        L.append("")
        L.append("| 并发 | 路径 | req/s | p50 ms | p90 ms | p99 ms | 失败请求 |")
        L.append("| ---: | --- | ---: | ---: | ---: | ---: | ---: |")
        for case in macro["cases"]:
            for label in ("baseline", "pswitch"):
                r = case[label]
                L.append(
                    f"| {case['concurrency']} | {label} "
                    f"| {fmt(r.get('requests_per_sec'), 0)} "
                    f"| {fmt(r.get('p50_ms'), 2)} | {fmt(r.get('p90_ms'), 2)} "
                    f"| {fmt(r.get('p99_ms'), 2)} "
                    f"| {r.get('failed_requests', '-')} |")
        L.append("")
        L.append("> ab 是闭环压测，且分位数只有 1 ms 精度（`0.00` 表示亚毫秒）；"
                 "这里主要看同一并发下两条路径的吞吐差异，"
                 "微秒级尾延迟看上面的“并发下的稳态 RTT”。")
        L.append("")
    elif macro and macro.get("skipped"):
        L.append(f"## 宏基准\n\n跳过：{macro['skipped']}\n")

    failover = results.get("failover")
    if failover:
        L.append("## 故障切换")
        L.append("")
        L.append("| 场景 | connect_timeout | 首包/EOF p50 µs | 首包/EOF p99 µs | 失败 |")
        L.append("| --- | ---: | ---: | ---: | --- |")
        for case in failover["cases"]:
            total = case.get("total") or {}
            L.append(
                f"| {case['case']} | {fmt(case.get('connect_timeout'), 1)}s "
                f"| {fmt(total.get('p50') if total else None)} "
                f"| {fmt(total.get('p99') if total else None)} "
                f"| {case.get('error', '-')} |")
        L.append("")
        bh = next((c for c in failover["cases"]
                   if c["case"] == "blackhole_failover"), None)
        if bh and bh.get("total") and bh["total"].get("p50") is not None:
            expected_us = bh["connect_timeout"] * 1_000_000
            got = bh["total"]["p50"]
            ok = 0.5 * expected_us <= got <= 1.5 * expected_us
            verdicts.append(
                (f"黑洞切换耗时 ≈ connect_timeout ({fmt(expected_us, 0)} µs)",
                 ok, f"{fmt(got)} µs"))

    e2e = results.get("e2e")
    if e2e and "delta_ms" in e2e:
        L.append("## 端到端配对差值（经 pswitch − 直连上游）")
        L.append("")
        L.append(f"- URL: `{e2e['url']}`")
        L.append(f"- 上游: `{e2e['upstream']}`，配对样本: {e2e['samples']}")
        L.append("")
        L.append("| 指标 | p50 ms | p90 ms | p99 ms | 均值 ms |")
        L.append("| --- | ---: | ---: | ---: | ---: |")
        for field, label in (("connect_ms", "TCP 建连"),
                             ("tls_ms", "TLS 握手"),
                             ("ttfb_ms", "首字节 TTFB"),
                             ("total_ms", "整请求")):
            s = e2e["delta_ms"][field]
            L.append(f"| {label} | {fmt(s['p50'], 2)} | {fmt(s['p90'], 2)} "
                     f"| {fmt(s['p99'], 2)} | {fmt(s['mean'], 2)} |")
        L.append("")
    elif e2e and e2e.get("skipped"):
        L.append(f"## 端到端\n\n跳过：{e2e['skipped']}\n")

    if verdicts:
        L.append("## 判定")
        L.append("")
        L.append("| 项 | 结果 | 实测 |")
        L.append("| --- | --- | ---: |")
        for label, ok, actual in verdicts:
            L.append(f"| {label} | {'OK' if ok else 'CHECK'} | {actual} |")
        L.append("")
        L.append("`CHECK` 不代表实现有错，先排除 CPU 频率、后台进程、"
                 "健康检查碰撞和 Nagle（p99 出现 ~40ms 台阶）再下结论。")
        L.append("")

    L.append("## 复现")
    L.append("")
    L.append("```bash")
    extra = ""
    if args.impl != "python":
        extra += f" --impl {args.impl}"
    if args.binary:
        extra += f" --binary {args.binary}"
    L.append(f"python bench/bench.py {args.mode}{extra}"
             + (f" --out {env.root}" if args.out else ""))
    L.append("```")
    L.append("")

    return "\n".join(L), all(v[1] for v in verdicts)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def parse_list(text, cast=int):
    return [cast(x.strip()) for x in text.split(",") if x.strip()]


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="pswitch 统一延迟基准（独立实例，不碰生产 17000）")
    p.add_argument("mode", choices=["micro", "loaded", "throughput",
                                    "macro", "failover", "e2e", "all"])
    p.add_argument("--out", help="结果目录（默认 bench/results/<时间戳>）")
    p.add_argument("--impl", choices=["python", "rust"], default="python",
                   help="被测实现：python 或 rust（rust/ 下的 release 二进制）")
    p.add_argument("--binary",
                   help="覆盖被测 pswitch 可执行文件路径（需支持 serve 子命令）")
    p.add_argument("--offset", type=int, default=0,
                   help="所有测试端口统一加偏移，端口冲突时用（如 100）")
    p.add_argument("--rounds", type=int, default=3, help="微基准轮数")
    p.add_argument("--samples", type=int, default=1000,
                   help="每轮稳态 RTT 样本数")
    p.add_argument("--fresh-samples", type=int, default=100,
                   help="每轮新建连接样本数")
    p.add_argument("--warmup", type=int, default=50, help="丢弃的预热样本数")
    p.add_argument("--payloads", type=parse_list, default=[1, 1024, 65536],
                   help="逗号分隔的 payload 字节数")
    p.add_argument("--duration", type=float, default=8.0,
                   help="加载测试每档持续秒数")
    p.add_argument("--streams", type=parse_list, default=[0, 1, 2],
                   help="背景流数量列表")
    p.add_argument("--probe-interval", type=float, default=0.01,
                   help="加载测试探测间隔秒数")
    p.add_argument("--concurrent", type=parse_list, default=[1, 8, 64],
                   help="并发 RTT 测试的连接数列表")
    p.add_argument("--concurrent-requests", type=int, default=300,
                   help="并发 RTT 测试每条连接的请求数")
    p.add_argument("--throughput-generator", choices=["auto", "iperf3", "python"],
                   default="auto", help="吞吐测试的负载生成器")
    p.add_argument("--throughput-directions", type=lambda s: parse_list(s, str),
                   default=["send", "recv"],
                   help="逗号分隔的方向: send,recv,bidir")
    p.add_argument("--throughput-streams", type=parse_list, default=[1, 4],
                   help="并行流数量列表")
    p.add_argument("--throughput-rounds", type=int, default=2,
                   help="每个吞吐组合的轮数")
    p.add_argument("--throughput-duration", type=float, default=6.0,
                   help="iperf3 单轮时长秒数")
    p.add_argument("--throughput-omit", type=float, default=2.0,
                   help="iperf3 丢弃的预热秒数")
    p.add_argument("--throughput-window", default="1M",
                   help="iperf3 socket 窗口")
    p.add_argument("--requests", type=int, default=10000,
                   help="ab 每轮请求数")
    p.add_argument("--concurrency", type=parse_list, default=[1, 64],
                   help="ab 并发列表")
    p.add_argument("--failover-samples", type=int, default=5,
                   help="每个故障切换场景的样本数")
    p.add_argument("--connect-timeout", type=float, default=1.0,
                   help="故障切换测试用的 pswitch connect_timeout 秒数")
    p.add_argument("--blackhole", default="10.255.255.1:17090",
                   help="模拟黑洞上游的地址（丢 SYN）")
    p.add_argument("--upstream", help="e2e 用的真实上游 host:port")
    p.add_argument("--url", default="https://www.gstatic.com/generate_204",
                   help="e2e 目标 URL")
    p.add_argument("--e2e-samples", type=int, default=30,
                   help="e2e 配对样本数")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="curl 单次超时秒数")
    p.add_argument("--strict", action="store_true",
                   help="任一判定为 CHECK 时以退出码 1 结束")
    p.add_argument("--no-cleanup", action="store_true",
                   help="结束后保留测试进程，便于手工排查")
    return p.parse_args(argv)


def git_commit():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO), capture_output=True, text=True).stdout.strip() or "-"
    except Exception:
        return "-"


def pswitch_version():
    if BIN_PATH:
        try:
            out = subprocess.run([BIN_PATH, "version"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            return out.replace("pswitch ", "") or "-"
        except Exception:
            return "-"
    try:
        text = (SRC / "pswitch" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else "-"
    except OSError:
        return "-"


def main(argv=None):
    global IMPL, BIN_PATH
    args = parse_args(argv)
    if args.mode == "e2e" and not args.upstream:
        raise SystemExit("e2e 需要 --upstream host:port")

    IMPL = args.impl
    candidate = args.binary or (str(RUST_BIN) if args.impl == "rust" else None)
    if candidate:
        if not Path(candidate).is_file():
            raise SystemExit(
                f"找不到被测可执行文件: {candidate}\n"
                f"先构建: cargo build --release --manifest-path rust/Cargo.toml")
        BIN_PATH = candidate
    else:
        BIN_PATH = None

    env = Env(args)
    meta = {
        "plan_version": "v1",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "pswitch_version": pswitch_version(),
        "impl": IMPL,
        "binary": BIN_PATH,
        "git_commit": git_commit(),
        "argv": sys.argv[1:],
        "ports": {
            "forward": env.forward_port,
            "direct": env.direct_port,
            "chain": env.chain_port,
            "raw_target": env.raw_port,
            "http_target": env.http_port,
            "http_forward": env.http_forward_port,
        },
    }
    (env.root / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    results = {}
    try:
        if args.mode in ("micro", "all", "loaded"):
            env.start_core()
        if args.mode in ("micro", "all"):
            results["micro"] = run_micro(env, args)
            env.save("micro", results["micro"])
        if args.mode in ("loaded", "all"):
            results["loaded"] = run_loaded(env, args)
            env.save("loaded", results["loaded"])
        if args.mode in ("throughput", "all"):
            results["throughput"] = run_throughput(env, args)
            env.save("throughput", results["throughput"])
        if args.mode in ("macro", "all"):
            results["macro"] = run_macro(env, args)
            env.save("macro", results["macro"])
        if args.mode in ("failover", "all"):
            results["failover"] = run_failover(env, args)
            env.save("failover", results["failover"])
        if args.mode in ("e2e", "all") and args.upstream:
            results["e2e"] = run_e2e(env, args)
            env.save("e2e", results["e2e"])
    except KeyboardInterrupt:
        print("\n中断，正在清理…", file=sys.stderr)
    except Exception as e:
        import traceback
        traceback.print_exc()
        (env.root / "error.txt").write_text(
            traceback.format_exc(), encoding="utf-8")
        print(f"测试失败: {e}", file=sys.stderr)
    finally:
        if not args.no_cleanup:
            env.cleanup()

    summary, all_ok = build_summary(env, args, meta, results)
    (env.root / "SUMMARY.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"\n结果目录: {env.root}")

    if args.strict and not all_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
