#!/usr/bin/env python3
"""bench 用的本地测试目标端，只依赖标准库。

两种模式：

raw（默认）——按客户端发来的首字节分流，给微基准和加载测试用：
    E + payload   原样回显 payload（测 RTT）
    S + stream    读走并丢弃（制造背景负载）
    D            持续下发 64 KiB 块，直到客户端断开（测下行吞吐）
    G/H ...       回一个 HTTP 204 后关闭（给 pswitch 健康探活用）

http——最小 keep-alive HTTP 服务，给 ab / 手工宏基准用：
    GET/HEAD 一律返回 200 + 3 字节正文，除非请求声明 Connection: close。

单独运行：
    python bench/target.py --port 18080
    python bench/target.py --port 18081 --mode http
"""

import argparse
import asyncio


# --------------------------------------------------------------------------
# raw 模式
# --------------------------------------------------------------------------

async def handle_raw(reader, writer):
    try:
        first = await reader.readexactly(1)
        if first in (b"G", b"H"):
            # pswitch 的探活请求是真实 HTTP；读到请求头就回 204。
            try:
                await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                pass
            writer.write(
                b"HTTP/1.1 204 No Content\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 0\r\n\r\n")
            await writer.drain()
        elif first == b"E":
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        elif first == b"S":
            while await reader.read(65536):
                pass
        elif first == b"D":
            chunk = b"\x00" * 65536
            while True:
                writer.write(chunk)
                await writer.drain()
        # 其它首字节：不认识，直接断开
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# http 模式
# --------------------------------------------------------------------------

async def handle_http(reader, writer):
    body = b"ok\n"
    try:
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                break

            lines = head.split(b"\r\n")
            try:
                method = lines[0].split(b" ")[0].upper()
            except IndexError:
                break
            lowered = head.lower()
            keep_alive = b"connection: close" not in lowered

            # 请求带 body 时先读掉，保持连接可复用。
            try:
                length = int(
                    next(line.split(b":", 1)[1].strip()
                         for line in lines
                         if line.lower().startswith(b"content-length:")))
            except (StopIteration, ValueError):
                length = 0
            if length:
                try:
                    await reader.readexactly(length)
                except asyncio.IncompleteReadError:
                    break

            payload = b"" if method == "HEAD" else body
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                + (b"Connection: keep-alive\r\n" if keep_alive
                   else b"Connection: close\r\n")
                + b"\r\n" + payload)
            await writer.drain()
            if not keep_alive:
                break
    except ConnectionError:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# --------------------------------------------------------------------------

async def main(args):
    handler = handle_http if args.mode == "http" else handle_raw
    server = await asyncio.start_server(
        handler, args.host, args.port, reuse_address=True)
    print(f"target ready {args.host}:{args.port} mode={args.mode}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="pswitch bench 测试目标端")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18080)
    p.add_argument("--mode", choices=["raw", "http"], default="raw")
    asyncio.run(main(p.parse_args()))
