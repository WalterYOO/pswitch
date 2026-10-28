"""pswitch 命令行接口。"""

import argparse
import asyncio
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

from . import __version__, completion, launchd
from .config import (
    CONFIG_PATH,
    HOME,
    LOG_PATH,
    STATE_PATH,
    config_hint,
    enabled_upstreams,
    ensure_config,
    find_upstream,
    has_upstreams,
    load_config,
    read_json,
    save_config,
    split_hostport,
)
from .proxy import daemon_running, port_open, probe_full, serve_main


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def signal_daemon(sig):
    pid = daemon_running()
    if pid:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def require_upstreams(cfg, action):
    if has_upstreams(cfg):
        return True
    print(f"还没有配置任何上游，拒绝{action}。")
    print("先添加一个，例如:")
    print("  pswitch add local 127.0.0.1:17890")
    print(config_hint())
    return False


def _live_probe(cfg):
    """只探活启用中的上游，返回值与 enabled_upstreams(cfg) 一一对应。"""
    async def _all():
        url = cfg.get("probe_url")
        timeout = cfg.get("probe_timeout", 3)
        return await asyncio.gather(*[probe_full(u["addr"], url, timeout)
                                      for u in enabled_upstreams(cfg)])
    return asyncio.run(_all())


def _auto_pick(ups, probes):
    """auto 选路：先 HTTP 通，再 TCP 通，最后退回第一个。"""
    for key in ("http", "tcp"):
        for u, h in zip(ups, probes):
            if h[key]:
                return u["name"]
    return ups[0]["name"] if ups else None


def require_enabled_upstreams(cfg, action):
    if enabled_upstreams(cfg):
        return True
    print(f"所有上游都被禁用了，拒绝{action}。")
    print("先启用一个，例如: pswitch enable <名字>")
    print(config_hint())
    return False


# --------------------------------------------------------------------------
# 进程管理
# --------------------------------------------------------------------------

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_start(args):
    cfg = load_config()
    if cfg.get("mode") != "direct":
        if not require_upstreams(cfg, "启动"):
            return 1
        if not require_enabled_upstreams(cfg, "启动"):
            return 1
    pid = daemon_running()
    if pid:
        print(f"已经在运行 (pid {pid})")
        return 0
    HOME.mkdir(parents=True, exist_ok=True)
    host, port = split_hostport(cfg["listen"])

    if launchd.installed():
        r = launchd.bootstrap()
        if r.returncode != 0 and "already bootstrapped" not in (r.stderr or ""):
            print(f"launchctl 启动失败: {(r.stderr or '').strip()}")
            return 1
    else:
        out = open(LOG_PATH, "a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, "-m", "pswitch", "serve"],
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
    if launchd.installed():
        # 先摘掉 launchd 作业，否则 KeepAlive 会立刻把它拉起来
        launchd.bootout()
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
    return 0


def cmd_restart(args):
    cmd_stop(args)
    return cmd_start(args)


# --------------------------------------------------------------------------
# 状态 / 上游管理
# --------------------------------------------------------------------------

def cmd_status(args):
    cfg = load_config()
    st = read_json(STATE_PATH) or {}
    pid = daemon_running()
    host, port = split_hostport(cfg["listen"])
    listening = port_open(host, port) if pid else False

    print("pswitch")
    print(f"  配置文件  {CONFIG_PATH}")
    if pid:
        print(f"  守护进程  运行中 (pid {pid})")
    else:
        print("  守护进程  未运行  → pswitch start")
    print(f"  监听      {cfg['listen']}  {'可连接' if listening else '不可连接'}")

    mode = cfg.get("mode")
    direct = mode == "direct"

    if not has_upstreams(cfg):
        if direct:
            print("  模式      direct 全部直连")
            print("  当前出口  direct（直接连接目标，不使用上游）")
        print("  上游      未配置  → pswitch add <名字> <host:port>")
        return 0

    enabled = enabled_upstreams(cfg)
    probes = _live_probe(cfg)
    health = {u["name"]: h for u, h in zip(enabled, probes)}

    if direct:
        print("  模式      direct 全部直连")
        print("  当前出口  direct（直接连接目标，不使用上游）")
        active = None
    else:
        interval = max(2, int(cfg.get("health_check_interval", 10) or 10))
        fresh = pid and (time.time() - st.get("updated", 0)) < max(15, 3 * interval)
        # 守护进程的 active 若指向刚被禁用的上游，就不算新鲜
        if fresh and st.get("mode") == mode and st.get("active") in health:
            active = st.get("active")
        elif mode == "manual" and cfg.get("active") in health:
            active = cfg["active"]
        else:
            active = _auto_pick(enabled, probes)

        print(f"  模式      {'manual 固定' if mode == 'manual' else 'auto 自动选择'}")
        if not enabled:
            print("  当前上游  -（所有上游都已禁用）")
        else:
            print(f"  当前上游  {active or '-'}")
            manual_target = cfg.get("active")
            if mode == "manual" and manual_target and manual_target not in health:
                print(f"  说明      manual 目标 {manual_target} 已禁用或不存在，按 auto 顺序选择")
    print("  上游列表")
    for u in cfg["upstreams"]:
        mark = "*" if u["name"] == active else " "
        if not u["enabled"]:
            print(f"   {mark} {u['name']:<12} {u['addr']:<24} 已禁用")
            continue
        h = health.get(u["name"], {})
        if h.get("http"):
            state = f"OK        {h.get('ms') or 0:>3}ms"
        elif h.get("tcp"):
            state = "TCP only"
        else:
            state = "DOWN"
        print(f"   {mark} {u['name']:<12} {u['addr']:<24} {state}")
    return 0


def cmd_use(args):
    cfg = load_config()
    key = args.target
    if key == "direct":
        cfg["mode"] = "direct"
        save_config(cfg)
        signal_daemon(signal.SIGHUP)
        print("已切换为 direct：所有请求直接连目标，不经过任何上游")
        return 0
    if not has_upstreams(cfg):
        print("还没有配置任何上游。先 pswitch add <名字> <host:port>")
        return 1
    if key == "auto":
        cfg["mode"] = "auto"
        save_config(cfg)
        signal_daemon(signal.SIGHUP)
        print("已切换为 auto：按顺序自动选择第一个可用的上游")
        return 0
    u = find_upstream(cfg, key)
    if not u:
        names = ", ".join(x["name"] for x in enabled_upstreams(cfg)) or "无（都被禁用了）"
        print(f"没有这个上游: {key}\n可选: {names}, auto, direct")
        return 1
    if not u["enabled"]:
        print(f"{u['name']} 已被禁用，先 pswitch enable {u['name']}")
        return 1
    cfg["mode"] = "manual"
    cfg["active"] = u["name"]
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    print(f"已切换: {u['name']} -> {u['addr']}")
    return 0


def cmd_list(args):
    cfg = load_config()
    if not has_upstreams(cfg):
        print("还没有配置任何上游。用 pswitch add <名字> <host:port> 添加。")
        return 0
    active = (read_json(STATE_PATH) or {}).get("active")
    for i, u in enumerate(cfg["upstreams"], 1):
        mark = "*" if u["name"] == active else " "
        tag = "" if u["enabled"] else "  已禁用"
        print(f" {mark} {i}. {u['name']:<12} {u['addr']}{tag}")
    return 0


def cmd_add(args):
    cfg = load_config()
    try:
        split_hostport(args.addr)
    except ValueError as e:
        print(e)
        return 1
    if find_upstream(cfg, args.name):
        print(f"名字已存在: {args.name}")
        return 1
    cfg["upstreams"].append({"name": args.name, "addr": args.addr, "enabled": True})
    if not cfg.get("active"):
        cfg["active"] = args.name
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    print(f"已添加: {args.name} -> {args.addr}")
    if len(cfg["upstreams"]) == 1:
        print("现在可以启动了: pswitch start")
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
        remaining = enabled_upstreams(cfg) or cfg["upstreams"]
        cfg["active"] = remaining[0]["name"] if remaining else None
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    print(f"已删除: {args.name}")
    return 0


def cmd_priority(args):
    cfg = load_config()
    ups = cfg["upstreams"]
    if not ups:
        print("还没有配置任何上游。用 pswitch add <名字> <host:port> 添加。")
        return 0
    if not args.name:
        print("优先级顺序（auto 从上到下取第一个可用的，禁用的跳过）：")
        for i, u in enumerate(ups, 1):
            tag = "" if u["enabled"] else "  已禁用"
            print(f" {i}. {u['name']:<12} {u['addr']:<24}{tag}".rstrip())
        return 0

    u = find_upstream(cfg, args.name)
    if not u:
        names = ", ".join(x["name"] for x in ups)
        print(f"没有这个上游: {args.name}\n可选: {names}")
        return 1
    cur = ups.index(u)
    if args.pos is None:
        print(f"{u['name']} 当前优先级 {cur + 1}/{len(ups)}")
        return 0
    if not 1 <= args.pos <= len(ups):
        print(f"位置要在 1..{len(ups)} 之间")
        return 1
    if args.pos - 1 == cur:
        print(f"{u['name']} 已经在第 {args.pos} 位")
        return 0
    ups.insert(args.pos - 1, ups.pop(cur))
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    print(f"已把 {u['name']} 排到第 {args.pos} 位: "
          + " > ".join(x["name"] for x in ups))
    if not u["enabled"]:
        print(f"注意：它当前是禁用状态，顺序不参与选路，先 pswitch enable {u['name']}")
    return 0


def _set_enabled(args, enabled):
    cfg = load_config()
    u = find_upstream(cfg, args.name)
    if not u:
        names = ", ".join(x["name"] for x in cfg["upstreams"]) or "无"
        print(f"没有这个上游: {args.name}\n可选: {names}")
        return 1
    if u["enabled"] == enabled:
        print(f"{u['name']} 已经是{'启用' if enabled else '禁用'}状态")
        return 0
    u["enabled"] = enabled
    save_config(cfg)
    signal_daemon(signal.SIGHUP)
    if enabled:
        print(f"已启用: {u['name']} -> {u['addr']}")
        return 0
    print(f"已禁用: {u['name']} -> {u['addr']}（配置保留，不参与 use / auto / 故障转移）")
    if not enabled_upstreams(cfg):
        if cfg.get("mode") == "direct":
            print("警告：所有上游都已禁用；direct 模式不受影响，切回 auto/manual 前需启用一个。")
        else:
            print("警告：所有上游都已禁用，转发会全部失败。启用一个: pswitch enable <名字>")
    elif cfg.get("mode") == "manual" and cfg.get("active") == u["name"]:
        print("注意：它正是当前 manual 固定的上游，守护进程会暂时改用其它可用上游。")
    return 0


def cmd_enable(args):
    return _set_enabled(args, True)


def cmd_disable(args):
    return _set_enabled(args, False)


# --------------------------------------------------------------------------
# 其他命令
# --------------------------------------------------------------------------

def cmd_version(args):
    print(f"pswitch {__version__}")
    return 0


def cmd_completion(args):
    return completion.run(args.shell, print_only=args.print_script,
                          uninstall_mode=args.uninstall)


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
    if cfg.get("mode") != "direct" and not require_upstreams(cfg, "测试"):
        return 1
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


def cmd_install(args):
    cfg = load_config()
    if cfg.get("mode") != "direct":
        if not require_upstreams(cfg, "安装开机自启"):
            return 1
        if not require_enabled_upstreams(cfg, "安装开机自启"):
            return 1
    if sys.platform != "darwin":
        print("仅支持 macOS 的 launchd")
        return 1
    launchd.write_plist()
    launchd.bootout()
    r = launchd.bootstrap()
    if r.returncode != 0 and "already bootstrapped" not in (r.stderr or ""):
        print(f"launchctl 失败: {(r.stderr or '').strip()}")
        return 1
    host, port = split_hostport(cfg["listen"])
    for _ in range(100):
        pid = daemon_running()
        if pid and port_open(host, port):
            print(f"已安装开机自启并启动 (pid {pid})，监听 {host}:{port}")
            print(f"plist: {launchd.PLIST}")
            print("停止并禁用: pswitch uninstall")
            return 0
        time.sleep(0.1)
    print(f"已安装开机自启: {launchd.PLIST}（进程尚未就绪，看日志 {LOG_PATH}）")
    print("停止并禁用: pswitch uninstall")
    return 0


def cmd_uninstall(args):
    launchd.bootout()
    launchd.remove_plist()
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


def _apply_system_proxy(svc, host, port):
    for cmd in (
        ["-setwebproxy", svc, host, str(port)],
        ["-setsecurewebproxy", svc, host, str(port)],
        ["-setsocksfirewallproxy", svc, host, str(port)],
        ["-setproxybypassdomains", svc, "127.0.0.1", "localhost"],
    ):
        subprocess.run(["networksetup", *cmd], capture_output=True)


def _resolve_system_target(cfg, target):
    """system use 的参数：上游名字优先，否则按 host:port 直接使用。"""
    for u in cfg["upstreams"]:
        if u["name"] == target:
            if not u["enabled"]:
                print(f"{u['name']} 已被禁用，先 pswitch enable {u['name']}")
                return None
            try:
                return split_hostport(u["addr"])
            except ValueError as e:
                print(e)
                return None
    try:
        return split_hostport(target)
    except ValueError:
        names = ", ".join(x["name"] for x in cfg["upstreams"]) or "无"
        print(f"没有这个上游也不是合法地址: {target}")
        print(f"可选: {names}，或直接给 host:port")
        return None


def _cmd_system_use(cfg, target):
    if not target:
        print("用法: pswitch system use <上游名字|host:port>")
        return 2
    resolved = _resolve_system_target(cfg, target)
    if not resolved:
        return 1
    svc = active_network_service()
    if not svc:
        print("找不到当前活动的网络服务")
        return 1
    host, port = resolved
    _apply_system_proxy(svc, host, port)
    u = next((x for x in cfg["upstreams"] if x["name"] == target), None)
    if u:
        print(f"系统代理（{svc}）已直接指向上游 {u['name']} ({host}:{port})")
    else:
        print(f"系统代理（{svc}）已直接指向 {host}:{port}")
    print("注意：这不经过 pswitch，use / 故障转移都不生效；恢复本地端口: pswitch system on")
    return 0


def cmd_system(args):
    cfg = load_config()
    if args.action == "use":
        return _cmd_system_use(cfg, args.target)
    if args.target:
        print(f"用法: pswitch system {args.action}")
        return 2
    if cfg.get("mode") != "direct" and not require_upstreams(cfg, "修改系统代理"):
        return 1
    if args.action == "on" and not daemon_running():
        print("守护进程未运行，先 pswitch start 或 pswitch install")
        return 1
    if (args.action == "on" and cfg.get("mode") != "direct"
            and not require_enabled_upstreams(cfg, "开启系统代理")):
        return 1
    svc = active_network_service()
    if not svc:
        print("找不到当前活动的网络服务")
        return 1
    host, port = split_hostport(cfg["listen"])
    if host == "0.0.0.0":
        host = "127.0.0.1"
    offs = [
        ["-setwebproxystate", svc, "off"],
        ["-setsecurewebproxystate", svc, "off"],
        ["-setsocksfirewallproxystate", svc, "off"],
    ]
    if args.action == "on":
        _apply_system_proxy(svc, host, port)
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
# 参数解析
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="pswitch", description="一个本地端口，多个上游代理")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="前台运行转发进程（调试用，通常由 start / launchd 调用）")
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
    sub.add_parser("version", help="查看版本号")

    u = sub.add_parser("use", help="切换出口：use <名字> | use auto | use direct")
    u.add_argument("target")

    a = sub.add_parser("add", help="添加上游")
    a.add_argument("name")
    a.add_argument("addr")

    r = sub.add_parser("remove", help="删除上游")
    r.add_argument("name")

    pr = sub.add_parser("priority", help="查看/调整优先级（auto 按此顺序选）")
    pr.add_argument("name", nargs="?", help="要调整的上游，省略则只列出顺序")
    pr.add_argument("pos", nargs="?", type=int, help="目标位置，1 = 最高优先级")

    en = sub.add_parser("enable", help="启用上游（保留配置，重新参与 use / auto 选路）")
    en.add_argument("name")

    dis = sub.add_parser("disable", help="禁用上游（保留配置，不参与 use / auto 选路）")
    dis.add_argument("name")

    t = sub.add_parser("test", help="经本地端口做一次真实请求")
    t.add_argument("--timeout", type=float, default=8)

    lg = sub.add_parser("logs", help="查看最近日志")
    lg.add_argument("-n", "--lines", type=int, default=30)

    sy = sub.add_parser("system", help="管理 macOS 系统代理指向")
    sy.add_argument("action", choices=["on", "off", "status", "use"])
    sy.add_argument("target", nargs="?",
                    help="use 的目标：上游名字或 host:port（不经过 pswitch 直接指向上游）")

    cp = sub.add_parser("completion", help="安装 shell 补全（zsh / bash / fish）")
    cp.add_argument("shell", nargs="?",
                    help="目标 shell，默认按 $SHELL 识别；--uninstall 省略时清理全部")
    cp.add_argument("--print", dest="print_script", action="store_true",
                    help="只把补全脚本打印到标准输出")
    cp.add_argument("--uninstall", action="store_true",
                    help="移除已安装的补全")

    return p


HANDLERS = {
    "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
    "status": cmd_status, "use": cmd_use, "list": cmd_list,
    "add": cmd_add, "remove": cmd_remove,
    "enable": cmd_enable, "disable": cmd_disable, "priority": cmd_priority,
    "env": cmd_env,
    "test": cmd_test, "logs": cmd_logs,
    "install": cmd_install, "uninstall": cmd_uninstall,
    "system": cmd_system, "version": cmd_version,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        return serve_main(args.foreground, args.debug)
    if args.cmd == "version":
        # 纯查询，不应该顺手创建配置文件
        return cmd_version(args)
    if args.cmd == "completion":
        # 只是装补全，同样不该创建配置文件
        return cmd_completion(args)
    if ensure_config():
        print(f"已创建配置文件: {CONFIG_PATH}（还没有上游，用 pswitch add 添加）")
    return HANDLERS[args.cmd](args)
