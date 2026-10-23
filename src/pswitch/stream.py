"""双向字节管道，转发模式和 direct 模式共用。"""

import asyncio


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
