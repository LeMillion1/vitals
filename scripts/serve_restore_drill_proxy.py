#!/usr/bin/env python3
"""Expose the internal restore app through a fixed credential-free byte proxy."""

from __future__ import annotations

import asyncio
import os


LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8000
TARGET_HOST = "vitals_app"
TARGET_PORT = 8000


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def _forward(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    try:
        server_reader, server_writer = await asyncio.open_connection(
            TARGET_HOST, TARGET_PORT
        )
    except (ConnectionError, OSError):
        client_writer.close()
        await client_writer.wait_closed()
        return
    await asyncio.gather(
        _copy(client_reader, server_writer),
        _copy(server_reader, client_writer),
    )


async def _serve() -> None:
    if os.getenv("VITALS_RESTORE_DRILL_PROXY") != "true":
        raise RuntimeError("restore drill proxy marker missing")
    server = await asyncio.start_server(_forward, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


def main() -> int:
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
