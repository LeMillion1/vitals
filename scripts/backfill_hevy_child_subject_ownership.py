#!/usr/bin/env python3
"""Inspect or advance the bounded Hevy-child ownership backfill.

The default invocation is read-only. ``--apply`` is required before one or
more independently committed batches may mutate data. This fixed Stage-3E
command exposes no table, phase, reset, delete, or database-URL selector.
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
from vitals.operations.ownership import hevy_child as backfill_service


OUTPUT_FORMAT_VERSION = 1
OPERATION = "hevy_child_subject_ownership_backfill"
DEFAULT_MAX_BATCHES = 1
MAX_BATCHES = 100

_LOWERCASE_SHA256 = re.compile(r"[0-9a-f]{64}")
_AGGREGATE_COUNT_KEYS = (
    "tables_total",
    "completed_tables",
    "snapshot_rows",
    "scanned_rows",
    "updated_rows",
    "unchanged_rows",
    "remaining_rows",
    "rows_above_high_watermark",
)
_BATCH_COUNT_KEYS = (
    "batch_scanned_rows",
    "batch_updated_rows",
    "batch_unchanged_rows",
)
_CHECKSUM_KEYS = (
    "data_checksum_before",
    "data_checksum_after",
    "ownership_checksum_after",
)


class _SafeArgumentError(ValueError):
    """A bounded parse failure whose value never contains caller argv."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _SafeArgumentError("invalid_arguments")


def _bounded_positive_int(*, option: str, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"{option} must be an integer from 1 to {maximum}"
            ) from None
        if isinstance(parsed, bool) or not 1 <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{option} must be an integer from 1 to {maximum}"
            )
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    """Return the fixed-target Hevy-child argument parser."""

    parser = _SafeArgumentParser(
        description=__doc__,
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="advance the fixed Hevy-child ownership checkpoint group",
    )
    parser.add_argument(
        "--batch-size",
        type=_bounded_positive_int(
            option="--batch-size",
            maximum=(
                backfill_service.MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE
            ),
        ),
        default=(
            backfill_service.DEFAULT_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE
        ),
        metavar=(
            "1.."
            f"{backfill_service.MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE}"
        ),
    )
    parser.add_argument(
        "--max-batches",
        type=_bounded_positive_int(option="--max-batches", maximum=MAX_BATCHES),
        default=DEFAULT_MAX_BATCHES,
        metavar=f"1..{MAX_BATCHES}",
    )
    return parser


def _nonnegative_count(safe: dict[str, Any], key: str) -> int:
    value = safe.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("backfill service returned an unsafe projection")
    return value


def _checksum(safe: dict[str, Any], key: str) -> str:
    value = safe.get(key)
    if not isinstance(value, str) or _LOWERCASE_SHA256.fullmatch(value) is None:
        raise RuntimeError("backfill service returned an unsafe projection")
    return value


def _catalog_tables() -> tuple[str, ...]:
    catalog = backfill_service.HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES
    if not isinstance(catalog, tuple) or catalog != (
        "hevy_exercises",
        "hevy_sets",
    ):
        raise RuntimeError("backfill service returned an unsafe projection")
    return catalog


def _success_payload(
    result: Any,
    *,
    mode: str,
    batch_size: int,
    max_batches: int,
    batches_processed: int,
) -> dict[str, Any]:
    """Apply the CLI's stricter no-ID/no-PHI allowlist."""

    safe = result.to_safe_dict()
    if not isinstance(safe, dict):
        raise RuntimeError("backfill service returned an unsafe projection")
    phase_key = safe.get("phase_key")
    if phase_key != backfill_service.HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE:
        raise RuntimeError("backfill service returned an unsafe projection")
    allowed_statuses = {
        item.value for item in backfill_service.HevyChildOwnershipBackfillStatus
    }
    if allowed_statuses != {
        "not_started",
        "running",
        "completed",
        "restore_blocked",
    }:
        raise RuntimeError("backfill service returned an unsafe projection")
    status = safe.get("status")
    if not isinstance(status, str) or status not in allowed_statuses:
        raise RuntimeError("backfill service returned an unsafe projection")

    payload: dict[str, Any] = {
        "format_version": OUTPUT_FORMAT_VERSION,
        "operation": OPERATION,
        "phase": phase_key,
        "mode": mode,
        "result": "ok",
        "status": status,
        "completed": status == "completed",
        "batch_size": batch_size,
        "max_batches": max_batches,
        "batches_processed": batches_processed,
    }
    for key in _AGGREGATE_COUNT_KEYS:
        payload[key] = _nonnegative_count(safe, key)
    if (
        payload["tables_total"] != len(_catalog_tables())
        or payload["completed_tables"] > payload["tables_total"]
        or payload["updated_rows"] + payload["unchanged_rows"]
        != payload["scanned_rows"]
        or payload["scanned_rows"] + payload["remaining_rows"]
        != payload["snapshot_rows"]
    ):
        raise RuntimeError("backfill service returned an unsafe projection")
    if status == "completed" and (
        payload["completed_tables"] != payload["tables_total"]
        or payload["remaining_rows"] != 0
    ):
        raise RuntimeError("backfill service returned an unsafe projection")
    for key in _CHECKSUM_KEYS:
        payload[key] = _checksum(safe, key)

    batch_table = safe.get("batch_table")
    if batch_table is not None:
        if not isinstance(batch_table, str) or batch_table not in _catalog_tables():
            raise RuntimeError("backfill service returned an unsafe projection")
    payload["batch_table"] = batch_table
    for key in _BATCH_COUNT_KEYS:
        payload[key] = _nonnegative_count(safe, key) if key in safe else 0
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
        if not args.apply:
            async with factory() as session:
                result = await backfill_service.preflight_hevy_child_ownership_backfill(
                    session
                )
                await session.rollback()
            return _success_payload(
                result,
                mode="status",
                batch_size=args.batch_size,
                max_batches=args.max_batches,
                batches_processed=0,
            )

        payload = None
        batches_processed = 0
        for _batch_number in range(args.max_batches):
            async with factory() as session:
                result = (
                    await backfill_service.run_hevy_child_ownership_backfill_batch(
                        session,
                        batch_size=args.batch_size,
                    )
                )
                candidate_payload = _success_payload(
                    result,
                    mode="apply",
                    batch_size=args.batch_size,
                    max_batches=args.max_batches,
                    batches_processed=batches_processed + 1,
                )
                await session.commit()
            batches_processed += 1
            payload = candidate_payload
            if payload["completed"]:
                break
        if payload is None:
            raise RuntimeError("backfill did not execute a batch")
        return payload
    finally:
        await _dispose_factory(factory)


def _typed_error_code(exc: Exception) -> str | None:
    typed_errors = (
        ("HevyChildOwnershipBackfillValidationError", "validation_error"),
        ("HevyChildOwnershipBackfillIdentityError", "identity_error"),
        ("HevyChildOwnershipBackfillDependencyError", "dependency_error"),
        ("HevyChildOwnershipBackfillStateError", "state_error"),
        ("HevyChildOwnershipBackfillProvenanceError", "provenance_error"),
    )
    for class_name, code in typed_errors:
        error_type = getattr(backfill_service, class_name, None)
        if isinstance(error_type, type) and isinstance(exc, error_type):
            return code
    base_type = getattr(backfill_service, "HevyChildOwnershipBackfillError", None)
    if isinstance(base_type, type) and isinstance(exc, base_type):
        return "backfill_error"
    return None


def _error_payload(*, mode: str, code: str) -> dict[str, Any]:
    return {
        "format_version": OUTPUT_FORMAT_VERSION,
        "operation": OPERATION,
        "phase": backfill_service.HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
        "mode": mode,
        "result": "error",
        "error_code": code,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the operator boundary and return a process exit status."""

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
        exit_code = 2 if typed_code == "validation_error" else 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
