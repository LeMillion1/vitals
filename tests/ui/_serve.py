"""Start the app the way a developer's machine does, for a browser to drive.

A module rather than a uvicorn command line because the local server is not
quite the app: it substitutes FakeRedis for a real one, exactly as
``run_local.py`` does. Without that the app runs against a Redis that is not
there, and what that produces is not a crash — it is a page that renders,
answers 200, and is quietly missing what was just written to it. A harness that
degrades silently is worse than one that refuses to start, because every defect
it invents looks like a defect in the product.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys

import fakeredis.aioredis
import uvicorn

import web.deps
from vitals.scheduler import jobs as scheduler_jobs
from vitals.scheduler.scheduler import clear_jobs


def _register_no_background_jobs(_settings=None) -> None:
    """The browser suite exercises requests, not clocks or provider networks."""

    clear_jobs()


scheduler_jobs.register_all_jobs = _register_no_background_jobs


_original_getaddrinfo = socket.getaddrinfo
_OriginalSocket = socket.socket


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback(host):
        raise OSError("the UI test server forbids external network resolution")
    return _original_getaddrinfo(host, *args, **kwargs)


class _LocalOnlySocket(_OriginalSocket):
    def connect(self, address):
        if isinstance(address, tuple) and not _is_loopback(address[0]):
            raise OSError("the UI test server forbids external network connections")
        return super().connect(address)


socket.getaddrinfo = _local_getaddrinfo
socket.socket = _LocalOnlySocket

web.deps._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

if __name__ == "__main__":
    uvicorn.run(
        "web.main:app",
        host="127.0.0.1",
        port=int(sys.argv[1]),
        log_level="warning",
        access_log=False,
    )
    del os
