"""Shared response boundary for sensitive JSON downloads."""

from fastapi.responses import Response

PRIVATE_DOWNLOAD_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}


def private_json_download(*, body: str, filename: str) -> Response:
    """Return sensitive JSON without leaving a reusable browser/proxy copy."""

    return Response(
        content=body,
        media_type="application/json",
        headers={
            **PRIVATE_DOWNLOAD_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
