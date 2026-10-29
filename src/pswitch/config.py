"""配置文件与运行时状态的读写。"""

import copy
import ipaddress
import json
import os
from pathlib import Path

HOME = Path(os.environ.get("PSWITCH_HOME") or (Path.home() / ".config" / "proxy_switch"))
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"
PID_PATH = HOME / "pswitch.pid"
LOG_PATH = HOME / "pswitch.log"

# 注意：这里不内置任何上游。首次运行只会生成一个空列表，
# 由用户通过 `pswitch add <名字> <host:port>` 添加。
# 每个上游是 {"name", "addr", "enabled"}；enabled=false 表示保留配置但不参与
# use / auto 选路（用 `pswitch enable|disable <名字>` 切换）。
DEFAULT_CONFIG = {
    "listen": "127.0.0.1:17000",
    # auto = 按顺序取第一个可用的；manual = 固定 active；
    # direct = 不经任何上游，直接把请求连到目标
    "mode": "auto",
    "active": None,
    "health_check_interval": 10,          # 秒
    "connect_timeout": 3,                 # 连接上游超时（秒）
    "failover": True,                     # 当前上游连不上时临时借用其它可用上游
    "probe_url": "http://connectivitycheck.gstatic.com/generate_204",
    "probe_timeout": 3,                   # HTTP 探活超时（秒）
    # 监听非本机地址时允许的来源（IP 或网段）。为空表示只接受本机连接；
    # 回环地址（127.0.0.1 / ::1）始终允许，不受此列表限制。
    "allow_cidrs": [],
    # 拒绝来源（IP 或网段），优先级高于 allow_cidrs；回环地址同样不受限制。
    "deny_cidrs": [],
    "upstreams": [],
}


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
    """没有配置文件就创建一份（不含任何上游）。返回是否新建。"""
    HOME.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        return False
    write_json(CONFIG_PATH, DEFAULT_CONFIG)
    return True


def load_config():
    ensure_config()
    raw = read_json(CONFIG_PATH) or {}
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(raw)
    cfg["upstreams"] = [
        # 缺少 enabled 字段（旧配置）视为启用；只有显式 false 才算禁用
        {"name": u["name"], "addr": u["addr"], "enabled": u.get("enabled") is not False}
        for u in (cfg.get("upstreams") or [])
        if isinstance(u, dict) and u.get("name") and u.get("addr")
    ]
    return cfg


def save_config(cfg):
    write_json(CONFIG_PATH, cfg)


def find_upstream(cfg, key):
    for u in cfg["upstreams"]:
        if u["name"] == key or u["addr"] == key:
            return u
    return None


def has_upstreams(cfg):
    return bool(cfg.get("upstreams"))


def enabled_upstreams(cfg):
    """参与选路的上游：被 disable 的保留在配置里但完全跳过。"""
    return [u for u in cfg.get("upstreams") or [] if u.get("enabled", True)]


def config_hint():
    return f"配置文件: {CONFIG_PATH}"


# --------------------------------------------------------------------------
# LAN 监听 / 允许来源
# --------------------------------------------------------------------------

_CIDR_EXAMPLE = "示例: 192.168.3.40 / 192.168.3.0/24"


def normalize_cidr(entry):
    """把 IP 或网段规范成 network 形式：192.168.3.40 -> 192.168.3.40/32。"""
    text = str(entry).strip()
    if not text:
        raise ValueError("空的 IP / 网段")
    try:
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError:
        raise ValueError(f"不是合法的 IP 或网段: {text}（{_CIDR_EXAMPLE}）") from None


def normalize_cidrs(entries):
    """规范化一组条目并去重，保持输入顺序。"""
    out = []
    for entry in entries:
        net = normalize_cidr(entry)
        if net not in out:
            out.append(net)
    return out


def peer_allowed(allow_cidrs, deny_cidrs, ip):
    """来源 IP 是否允许：回环始终允许；deny 优先于 allow；非法项忽略。"""
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    for entry in deny_cidrs or []:
        try:
            if addr in ipaddress.ip_network(str(entry), strict=False):
                return False
        except ValueError:
            continue
    for entry in allow_cidrs or []:
        try:
            if addr in ipaddress.ip_network(str(entry), strict=False):
                return True
        except ValueError:
            continue
    return False


def is_loopback_host(host):
    """监听地址是否只面向本机。"""
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False
