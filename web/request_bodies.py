"""Bounded request-body parsing shared by security-sensitive JSON endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request, status


async def read_bounded_json_object(
    request: Request,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """Read one JSON object while enforcing media type and streaming size caps."""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type.lower() != "application/json":
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)

    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
            if declared_size < 0:
                raise ValueError
            if declared_size > max_bytes:
                raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return value


__all__ = ["read_bounded_json_object"]
