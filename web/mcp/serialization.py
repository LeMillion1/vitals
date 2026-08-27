"""JSON-safe serialization conventions for Vitals MCP tool results."""

from __future__ import annotations

from typing import Any


# Cross-surface ownership and private-resource plumbing. The portability surface
# owns the original policy; the architecture contract keeps this delivery-leaf
# copy exactly aligned without making a small serializer import that monolith.
_GENERIC_OUTPUT_SUPPRESSED_COLUMNS = frozenset(
    {
        "subject_id",
        "actor_user_id",
        "created_by_user_id",
        "revoked_by_user_id",
        "overridden_by_user_id",
        "resolved_by_user_id",
        "recipient_user_id",
        "requested_by_user_id",
        "integration_connection_id",
        "ai_invocation_id",
        "delivery_intent_id",
        "file_asset_id",
        "uploaded_by_user_id",
        "credential_ref",
        "storage_ref",
        "opaque_key",
    }
)

# Columns every row carries and no tool ever accepts back: bookkeeping the model
# cannot act on. ``id``, ``date`` and ``source`` intentionally stay public.
_ROW_NOISE = (
    frozenset(
        {
            "domain",
            "created_at",
            "updated_at",
            "raw_payload_id",
            "raw_id",
            "ai_invocation_id",
            "weight_log_id",
        }
    )
    | _GENERIC_OUTPUT_SUPPRESSED_COLUMNS
)


def serialize_row(row) -> dict:
    """Convert a SQLAlchemy model row into the compact public MCP shape."""

    if row is None:
        return {}
    payload = {}
    for column in row.__table__.columns:
        if column.name in _ROW_NOISE:
            continue
        value = getattr(row, column.name)
        if value is None:
            continue
        payload[column.name] = (
            value.isoformat() if hasattr(value, "isoformat") else value
        )
    return payload


async def serialize_written(session, row) -> dict:
    """Refresh a written row before compact serialization."""

    if row is None:
        return {}
    await session.refresh(row)
    return serialize_row(row)


def _conflict_payload(exc: Any) -> dict:
    """Return the stable MCP representation of a blocked conflict write."""

    return {
        "blocked": True,
        "message": str(exc),
        "violations": [violation.to_dict() for violation in exc.violations],
        "hint": "Retry the same call with override=True to save anyway.",
    }
