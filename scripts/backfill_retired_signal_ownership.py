#!/usr/bin/env python3
"""Inspect or attribute the retired ``signals`` and ``day_context`` tables."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from vitals.config import load_config
from vitals.database import create_session_factory
from vitals.operations.ownership import retired_signals as service

MAX_BATCHES = 100


class _SafeArgumentError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _SafeArgumentError("invalid_arguments")


def _bounded(value: str, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("value is outside the bounded range") from None
    if not 1 <= parsed <= maximum:
        raise argparse.ArgumentTypeError("value is outside the bounded range")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--batch-size",
        type=lambda value: _bounded(
            value,
            maximum=service.MAX_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE,
        ),
        default=service.DEFAULT_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE,
    )
    parser.add_argument(
        "--max-batches",
        type=lambda value: _bounded(value, maximum=MAX_BATCHES),
        default=1,
    )
    return parser


async def _dispose(factory) -> None:
    engine = factory.kw.get("bind")
    if engine is not None:
        await engine.dispose()


async def _execute(args: argparse.Namespace) -> dict[str, object]:
    factory = create_session_factory(load_config())
    updated_rows = 0
    batches_processed = 0
    try:
        if not args.apply:
            async with factory() as session:
                result = await service.inspect_retired_signal_ownership(session)
                await session.rollback()
        else:
            result = None
            for _ in range(args.max_batches):
                async with factory() as session:
                    result = (
                        await service.run_retired_signal_ownership_backfill_batch(
                            session, batch_size=args.batch_size
                        )
                    )
                    await session.commit()
                batches_processed += 1
                updated_rows += result.updated_rows
                if result.completed:
                    break
            if result is None:
                raise RuntimeError("retired signal backfill did not run")
        payload = result.to_safe_dict()
        payload.update(
            {
                "format_version": 1,
                "operation": "retired_signal_ownership_backfill",
                "phase": payload.pop("phase_key"),
                "mode": "apply" if args.apply else "status",
                "result": "ok",
                "completed": result.completed,
                "updated_rows": updated_rows if args.apply else 0,
                "batch_size": args.batch_size,
                "max_batches": args.max_batches,
                "batches_processed": batches_processed,
            }
        )
        return payload
    finally:
        await _dispose(factory)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except _SafeArgumentError:
        print('{"error_code":"invalid_arguments","result":"error"}')
        return 2
    try:
        payload = asyncio.run(_execute(args))
        code = 0
    except KeyboardInterrupt:
        payload = {"error_code": "cancelled", "result": "error"}
        code = 130
    except service.RetiredSignalOwnershipBackfillError:
        payload = {"error_code": "backfill_error", "result": "error"}
        code = 1
    except Exception:
        payload = {"error_code": "internal_error", "result": "error"}
        code = 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
