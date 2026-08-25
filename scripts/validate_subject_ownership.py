#!/usr/bin/env python3
"""Inspect or record the fixed Stage-4 whole-lake ownership validation.

The default invocation is read-only. ``--apply`` records the reviewed evidence
and, on PostgreSQL, makes the Stage-4 subject-equality foreign keys valid. No
table, phase, reset, delete, or database-URL selector is exposed, and the
operation never writes health data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from vitals.config import load_config
from vitals.database import create_session_factory
from vitals.operations.ownership import validate as validation_service


OUTPUT_FORMAT_VERSION = 1
OPERATION = "subject_ownership_validation"

_LOWERCASE_SHA256 = re.compile(r"[0-9a-f]{64}")
_AGGREGATE_COUNT_KEYS = (
    "tables_total",
    "checks_total",
    "rows_inspected",
    "violations_total",
    "validated_constraints",
)


class _SafeArgumentError(ValueError):
    """A bounded parse failure whose value never contains caller argv."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _SafeArgumentError("invalid_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description=__doc__,
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="record the reviewed whole-lake validation evidence",
    )
    return parser


def _nonnegative_count(safe: dict[str, Any], key: str) -> int:
    value = safe.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("validation service returned an unsafe projection")
    return value


def _success_payload(result: Any, *, mode: str) -> dict[str, Any]:
    """Apply a strict aggregate-only no-ID/no-PHI allowlist."""

    safe = result.to_safe_dict()
    if not isinstance(safe, dict):
        raise RuntimeError("validation service returned an unsafe projection")
    if safe.get("phase_key") != validation_service.OWNERSHIP_VALIDATION_PHASE:
        raise RuntimeError("validation service returned an unsafe projection")
    allowed_statuses = {
        item.value for item in validation_service.OwnershipValidationStatus
    }
    if allowed_statuses != {"not_started", "completed"}:
        raise RuntimeError("validation service returned an unsafe projection")
    status = safe.get("status")
    if not isinstance(status, str) or status not in allowed_statuses:
        raise RuntimeError("validation service returned an unsafe projection")
    checksum = safe.get("graph_checksum")
    if not isinstance(checksum, str) or _LOWERCASE_SHA256.fullmatch(checksum) is None:
        raise RuntimeError("validation service returned an unsafe projection")

    payload: dict[str, Any] = {
        "format_version": OUTPUT_FORMAT_VERSION,
        "operation": OPERATION,
        "phase": safe["phase_key"],
        "mode": mode,
        "result": "ok",
        "status": status,
        "completed": status == "completed",
        "graph_checksum": checksum,
    }
    for key in _AGGREGATE_COUNT_KEYS:
        payload[key] = _nonnegative_count(safe, key)
    if payload["violations_total"] != 0:
        raise RuntimeError("validation service returned an unsafe projection")
    if payload["checks_total"] < payload["tables_total"]:
        raise RuntimeError("validation service returned an unsafe projection")
    if status == "completed" and payload["tables_total"] == 0:
        raise RuntimeError("validation service returned an unsafe projection")
    return payload


async def _dispose_factory(factory: Any) -> None:
    options = getattr(factory, "kw", None)
    engine = options.get("bind") if isinstance(options, dict) else None
    dispose = getattr(engine, "dispose", None)
    if dispose is not None:
        await dispose()


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    factory = create_session_factory(load_config())
    try:
        async with factory() as session:
            if not args.apply:
                result = await validation_service.preflight_ownership_validation(
                    session
                )
                payload = _success_payload(result, mode="status")
                await session.rollback()
                return payload
            result = await validation_service.run_ownership_validation(session)
            payload = _success_payload(result, mode="apply")
            await session.commit()
            return payload
    finally:
        await _dispose_factory(factory)


def _typed_error_code(exc: Exception) -> str | None:
    typed_errors = (
        ("OwnershipValidationViolation", "violation"),
        ("OwnershipValidationIdentityError", "identity_error"),
        ("OwnershipValidationDependencyError", "dependency_error"),
        ("OwnershipValidationStateError", "state_error"),
    )
    for class_name, code in typed_errors:
        error_type = getattr(validation_service, class_name, None)
        if isinstance(error_type, type) and isinstance(exc, error_type):
            return code
    base_type = getattr(validation_service, "OwnershipValidationError", None)
    if isinstance(base_type, type) and isinstance(exc, base_type):
        return "validation_error"
    return None


def _error_payload(*, mode: str, code: str) -> dict[str, Any]:
    return {
        "format_version": OUTPUT_FORMAT_VERSION,
        "operation": OPERATION,
        "phase": validation_service.OWNERSHIP_VALIDATION_PHASE,
        "mode": mode,
        "result": "error",
        "error_code": code,
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except _SafeArgumentError:
        print(
            json.dumps(
                _error_payload(mode="argument", code="invalid_arguments"),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    mode = "apply" if args.apply else "status"
    exit_code = 0
    try:
        payload = asyncio.run(_execute(args))
    except KeyboardInterrupt:
        payload = _error_payload(mode=mode, code="cancelled")
        exit_code = 130
    except asyncio.CancelledError:
        payload = _error_payload(mode=mode, code="cancelled")
        exit_code = 1
    except Exception as exc:
        typed_code = _typed_error_code(exc)
        payload = _error_payload(mode=mode, code=typed_code or "internal_error")
        exit_code = 2 if typed_code == "violation" else 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
