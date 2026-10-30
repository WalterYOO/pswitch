"""双向字节管道，转发模式和 direct 模式共用。"""

import asyncio
import socket

# 对端掉线时不会有 FIN/RST，只有 keepalive 能把隧道判死并回收 fd
KEEPALIVE_IDLE = 30
KEEPALIVE_INTERVAL = 10
KEEPALIVE_COUNT = 3


def tune(writer):
    """关 Nagle、开 TCP keepalive。"""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle_opt = (getattr(socket, "TCP_KEEPALIVE", None)       # macOS
                    or getattr(socket, "TCP_KEEPIDLE", None))     # Linux
        if idle_opt is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle_opt, KEEPALIVE_IDLE)
        for name, value in (("TCP_KEEPINTVL", KEEPALIVE_INTERVAL),
                            ("TCP_KEEPCNT", KEEPALIVE_COUNT)):
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except OSError:
        pass


async def pipe(reader, writer):
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


async def relay(a_reader, a_writer, b_reader, b_writer):
    """a/b 两端对拷，结束后关闭两边。"""
    tune(a_writer)
    tune(b_writer)
    await asyncio.gather(
        pipe(a_reader, b_writer),
        pipe(b_reader, a_writer),
    )
    for w in (a_writer, b_writer):
        try:
            w.close()
        except Exception:
            pass


def close(writer):
    try:
        writer.close()
    except Exception:
        pass
