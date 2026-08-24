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

import os
import sys

import fakeredis.aioredis
import uvicorn

import web.deps

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
