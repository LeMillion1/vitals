"""Safe MCP error projection at the delivery boundary."""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from pathlib import Path

from mcp_types import CallToolResult, TextContent
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from web.mcp.arguments import McpArgumentError


class McpActorUnresolved(RuntimeError):
    """A token cannot identify a safe subject for an ownership-sensitive call."""


def visible_tool_failure(
    name: str,
    exc: Exception,
    *,
    output_schema: dict | None,
    logger: logging.Logger,
) -> CallToolResult:
    """Project an execution failure without exposing health data or credentials."""

    chain: list[BaseException] = []
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        chain.append(cause)
        cause = cause.__cause__

    classified = next(
        (
            item
            for item in chain
            if isinstance(item, McpActorUnresolved | PermissionError)
        ),
        None,
    )
    if classified is not None:
        code = "access_denied"
        message = "The connector is not authorized for this operation."
    elif classified := next(
        (item for item in chain if isinstance(item, SQLAlchemyError)), None
    ):
        code = "database_error"
        message = "The database could not complete the operation."
    elif classified := next(
        (item for item in chain if isinstance(item, ConnectionError | TimeoutError)),
        None,
    ):
        code = "dependency_unavailable"
        message = "A required service is temporarily unavailable."
    elif classified := next(
        (item for item in chain if isinstance(item, McpArgumentError)),
        None,
    ):
        code = "invalid_request"
        message = str(classified)
    elif classified := next(
        (item for item in chain if isinstance(item, ValidationError)), None
    ):
        code = "invalid_request"
        message = "The tool could not validate its arguments."
    else:
        classified = chain[-1]
        code = "internal_error"
        message = "The tool failed unexpectedly."

    error_id = uuid.uuid4().hex
    frames = [
        frame
        for item in chain
        for frame in traceback.extract_tb(item.__traceback__)
    ]
    application_frames = [
        frame for frame in frames if "site-packages" not in Path(frame.filename).parts
    ]
    if application_frames or frames:
        frame = (application_frames or frames)[-1]
        location = f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
    else:
        location = "unavailable"

    sqlstate = "none"
    constraint = "none"
    database_error = next(
        (item for item in chain if isinstance(item, SQLAlchemyError)), None
    )
    if database_error is not None:
        original = getattr(database_error, "orig", None)
        raw_sqlstate = getattr(original, "sqlstate", None) or getattr(
            original, "pgcode", None
        )
        if (
            isinstance(raw_sqlstate, str)
            and len(raw_sqlstate) == 5
            and all(
                character.isdigit() or "A" <= character <= "Z"
                for character in raw_sqlstate
            )
        ):
            sqlstate = raw_sqlstate
        raw_constraint = getattr(
            getattr(original, "diag", None), "constraint_name", None
        )
        if (
            isinstance(raw_constraint, str)
            and 0 < len(raw_constraint) <= 128
            and all(
                character.isalnum() or character == "_"
                for character in raw_constraint
            )
        ):
            constraint = raw_constraint
    logger.error(
        "mcp: tool execution failed error_id=%s tool=%s code=%s "
        "exception=%s location=%s sqlstate=%s constraint=%s",
        error_id,
        name,
        code,
        type(classified).__name__,
        location,
        sqlstate,
        constraint,
    )
    payload = {
        "error": message,
        "code": code,
        "error_id": error_id,
        # A transport or database failure after COMMIT has an unknown outcome.
        # Never invite an automatic retry that could duplicate a health write.
        "retryable": False,
    }
    structured_content: dict | None = payload
    if output_schema is not None:
        result_schema = output_schema.get("properties", {}).get("result", {})
        structured_content = (
            {"result": [payload]} if result_schema.get("type") == "array" else None
        )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, sort_keys=True))],
        structured_content=structured_content,
    )


__all__ = ["McpActorUnresolved", "visible_tool_failure"]
