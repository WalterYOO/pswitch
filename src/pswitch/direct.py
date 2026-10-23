"""direct 模式：pswitch 自己充当 HTTP / SOCKS5 代理，直接连接目标。

转发模式下 pswitch 只是盲转字节，由上游解释代理协议；direct 模式下没有上游，
所以这里要亲自完成 CONNECT / SOCKS5 握手，之后同样双向 pipe。

支持范围（方案 A）：
- HTTP CONNECT（HTTPS、以及任何隧道协议）
- 普通 HTTP 代理请求：改写首个请求行为 origin-form，并强制 Connection: close
  （代理客户端可能在同一连接上复用并切换目标，强制短连接可避免串目标）
- SOCKS5 CONNECT，无认证；BIND / UDP ASSOCIATE 明确拒绝
- SOCKS4 不支持（记录日志后断开）
"""

import asyncio
import logging
import socket
from urllib.parse import urlsplit

from .stream import close, relay

log = logging.getLogger("pswitch")

# 客户端发来代理请求头的等待上限；建连沿用 cfg["connect_timeout"]
HANDSHAKE_TIMEOUT = 10.0


class BadRequest(Exception):
    """客户端请求不合法，回一个明确的错误后断开。"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def split_hostport(s, default_port=None):
    """解析 host:port，支持 [IPv6]:port；没有端口时用 default_port。"""
    s = (s or "").strip()
    if not s:
        raise BadRequest("空地址")
    if s.startswith("["):
        end = s.find("]")
        if end < 0:
            raise BadRequest(f"地址格式错误: {s!r}")
        host = s[1:end]
        rest = s[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        if rest:
            raise BadRequest(f"地址格式错误: {s!r}")
        if default_port is None:
            raise BadRequest(f"缺少端口: {s!r}")
        return host, default_port
    host, sep, port = s.rpartition(":")
    if sep and host and port.isdigit():
        return host, int(port)
    if default_port is None:
        raise BadRequest(f"缺少端口: {s!r}")
    return s, default_port


async def _read_exact(reader, n):
    return await asyncio.wait_for(reader.readexactly(n), HANDSHAKE_TIMEOUT)


async def _read_head(reader):
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_TIMEOUT)
    except asyncio.IncompleteReadError as e:
        raise BadRequest("请求头未发送完整就断开") from e
    except asyncio.LimitOverrunError as e:
        raise BadRequest("请求头过长") from e


async def _dial(host, port, timeout):
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)


async def _send_http_error(writer, code, reason):
    try:
        writer.write(
            (f"HTTP/1.1 {code} {reason}\r\n"
             "Connection: close\r\n"
             "Content-Length: 0\r\n\r\n").encode("latin-1"))
        await writer.drain()
    except Exception:
        pass


def _socks5_reply(rep, sockname=None):
    ip, port = b"\x00\x00\x00\x00", 0
    if isinstance(sockname, tuple) and len(sockname) >= 2:
        try:
            ip, port = socket.inet_aton(sockname[0]), int(sockname[1])
        except (OSError, ValueError, TypeError):
            pass
    return b"\x05" + bytes([rep]) + b"\x00\x01" + ip + port.to_bytes(2, "big")


def _socks5_error(exc):
    if isinstance(exc, ConnectionRefusedError):
        return 0x05          # connection refused
    if isinstance(exc, socket.gaierror):
        return 0x04          # host unreachable
    if isinstance(exc, asyncio.TimeoutError):
        return 0x06          # TTL expired
    return 0x01              # general failure


# --------------------------------------------------------------------------
# HTTP 代理
# --------------------------------------------------------------------------

def _parse_head(raw):
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise BadRequest(f"请求行无法解析: {lines[0]!r}")
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise BadRequest(f"请求头无法解析: {line!r}")
        headers.append((name.strip(), value.strip()))
    return parts[0], parts[1], parts[2], headers


def _header(headers, key):
    for name, value in headers:
        if name.lower() == key:
            return value
    return None


def _origin_form(method, target, version, headers):
    """把代理请求目标改写为 origin-form，返回 (host, port, 请求行)。"""
    if target.startswith("http://"):
        u = urlsplit(target)
        if not u.hostname:
            raise BadRequest(f"URL 缺少主机名: {target!r}")
        host, port = u.hostname, u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        return host, port, f"{method} {path} {version}"
    if target.startswith("https://"):
        # 明文连接里没法对 https 目标做 TLS，正常客户端应该用 CONNECT
        raise BadRequest("https 请求必须使用 CONNECT")
    if target.startswith("/") or target == "*":
        host_header = _header(headers, "host")
        if not host_header:
            raise BadRequest("请求缺少 Host 头")
        host, port = split_hostport(host_header, 80)
        return host, port, f"{method} {target} {version}"
    raise BadRequest(f"无法识别的请求目标: {target!r}")


async def _handle_http(reader, writer, first, timeout, peer):
    try:
        head = first + await _read_head(reader)
        raw, _, _ = head.partition(b"\r\n\r\n")
        method, target, version, headers = _parse_head(raw)

        if method.upper() == "CONNECT":
            host, port = split_hostport(target)
            try:
                ureader, uwriter = await _dial(host, port, timeout)
            except Exception as e:
                log.warning("direct CONNECT %s:%s 失败: %s", host, port, e)
                await _send_http_error(writer, 502, "Bad Gateway")
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            log.debug("%s -> direct CONNECT %s:%s", peer, host, port)
            await relay(reader, writer, ureader, uwriter)
            return

        host, port, request_line = _origin_form(method, target, version, headers)
        # 逐跳头不发给源站；普通请求强制短连接，避免同一连接上切换目标。
        # 带 Upgrade 的请求（ws:// 等）保留连接语义，交给两端自行协商。
        upgrade = "upgrade" in (_header(headers, "connection") or "").lower()
        drop = {"proxy-connection", "proxy-authorization", "keep-alive"}
        if not upgrade:
            drop.add("connection")
        lines = [request_line]
        for name, value in headers:
            if name.lower() not in drop:
                lines.append(f"{name}: {value}")
        if _header(headers, "host") is None:
            lines.append(f"Host: {host}" + (f":{port}" if port != 80 else ""))
        if not upgrade:
            lines.append("Connection: close")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

        try:
            ureader, uwriter = await _dial(host, port, timeout)
        except Exception as e:
            log.warning("direct %s %s:%s 失败: %s", method, host, port, e)
            await _send_http_error(writer, 502, "Bad Gateway")
            return
        uwriter.write(request)
        await uwriter.drain()
        log.debug("%s -> direct %s %s:%s", peer, method, host, port)
        await relay(reader, writer, ureader, uwriter)
    except BadRequest as e:
        log.debug("%s direct HTTP 请求无效: %s", peer, e)
        await _send_http_error(writer, 400, "Bad Request")


# --------------------------------------------------------------------------
# SOCKS5
# --------------------------------------------------------------------------

async def _handle_socks5(reader, writer, timeout, peer):
    try:
        n = (await _read_exact(reader, 1))[0]
        methods = await _read_exact(reader, n)
        if 0x00 not in methods:
            writer.write(b"\x05\xff")      # 没有可接受的认证方式
            await writer.drain()
            return
        writer.write(b"\x05\x00")          # 无认证
        await writer.drain()

        hdr = await _read_exact(reader, 4)
        if hdr[0] != 5:
            raise BadRequest(f"SOCKS5 版本错误: {hdr[0]}")
        cmd, atyp = hdr[1], hdr[3]
        if atyp == 0x01:
            host = socket.inet_ntoa(await _read_exact(reader, 4))
        elif atyp == 0x03:
            size = (await _read_exact(reader, 1))[0]
            try:
                host = (await _read_exact(reader, size)).decode("ascii")
            except UnicodeDecodeError as e:
                raise BadRequest("域名不是 ASCII") from e
        elif atyp == 0x04:
            host = socket.inet_ntop(socket.AF_INET6, await _read_exact(reader, 16))
        else:
            writer.write(_socks5_reply(0x08))   # address type not supported
            await writer.drain()
            return
        port = int.from_bytes(await _read_exact(reader, 2), "big")

        if cmd != 0x01:                          # 只支持 CONNECT
            writer.write(_socks5_reply(0x07))    # command not supported
            await writer.drain()
            return

        try:
            ureader, uwriter = await _dial(host, port, timeout)
        except Exception as e:
            log.warning("direct SOCKS5 %s:%s 失败: %s", host, port, e)
            writer.write(_socks5_reply(_socks5_error(e)))
            await writer.drain()
            return

        writer.write(_socks5_reply(0x00, uwriter.get_extra_info("sockname")))
        await writer.drain()
        log.debug("%s -> direct SOCKS5 %s:%s", peer, host, port)
        await relay(reader, writer, ureader, uwriter)
    except BadRequest as e:
        log.debug("%s direct SOCKS5 请求无效: %s", peer, e)
        try:
            writer.write(_socks5_reply(0x01))
            await writer.drain()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

async def handle_direct(reader, writer, cfg, peer=None):
    """direct 模式下的单连接处理：嗅探协议后交给对应的服务端实现。"""
    timeout = cfg.get("connect_timeout", 3)
    try:
        first = await _read_exact(reader, 1)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        close(writer)
        return

    try:
        if first == b"\x05":
            await _handle_socks5(reader, writer, timeout, peer)
        elif first == b"\x04":
            log.warning("%s 使用 SOCKS4，direct 模式不支持", peer)
        else:
            await _handle_http(reader, writer, first, timeout, peer)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError,
            asyncio.LimitOverrunError, ConnectionError):
        log.debug("%s direct 连接中断", peer)
    except Exception as e:
        log.warning("%s direct 处理异常: %s", peer, e)
    finally:
        close(writer)
