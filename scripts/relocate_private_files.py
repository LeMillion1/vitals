#!/usr/bin/env python3
"""Inspect or relocate bounded legacy medical files into private storage.

The default is read-only. ``--apply`` commits at most one bounded batch. Output
contains aggregate counts and fixed result codes only — never file locators,
subjects, filenames, payloads, database URLs or private-root paths.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from vitals.config import load_config
from vitals.database import create_session_factory
from vitals.operations import file_storage_relocation

OUTPUT_FORMAT_VERSION = 1
OPERATION = "private_file_storage_relocation"
DEFAULT_PRIVATE_ROOT = "/data/private_files"


class _SafeArgumentError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _SafeArgumentError("invalid_arguments")


def _batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("batch size is invalid") from None
    if not 1 <= parsed <= file_storage_relocation.MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError("batch size is invalid")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=file_storage_relocation.DEFAULT_BATCH_SIZE,
    )
    return parser


async def _dispose_factory(factory: Any) -> None:
    options = getattr(factory, "kw", None)
    engine = options.get("bind") if isinstance(options, dict) else None
    dispose = getattr(engine, "dispose", None)
    if dispose is not None:
        await dispose()


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    private_root = os.path.realpath(
        os.getenv("VITALS_PRIVATE_FILE_ROOT", DEFAULT_PRIVATE_ROOT)
    )
    if not os.path.isabs(private_root):
        raise ValueError("private root must be absolute")
    static_dir = str(_REPOSITORY_ROOT / "web" / "static")
    factory = create_session_factory(load_config())
    try:
        if args.apply:
            result = await file_storage_relocation.relocate(
                factory,
                static_dir=static_dir,
                private_root=private_root,
                batch_size=args.batch_size,
            )
            return {
                "format_version": OUTPUT_FORMAT_VERSION,
                "operation": OPERATION,
                "mode": "apply",
                "result": "ok",
                **result.to_safe_dict(),
            }
        async with factory() as session:
            result = await file_storage_relocation.inspect(session)
            await session.rollback()
        return {
            "format_version": OUTPUT_FORMAT_VERSION,
            "operation": OPERATION,
            "mode": "status",
            "result": "ok",
            **result.to_safe_dict(),
        }
    finally:
        await _dispose_factory(factory)


def _error(mode: str, code: str) -> dict[str, str | int]:
    return {
        "format_version": OUTPUT_FORMAT_VERSION,
        "operation": OPERATION,
        "mode": mode,
        "result": "error",
        "error_code": code,
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except _SafeArgumentError:
        print(json.dumps(_error("argument", "invalid_arguments"), sort_keys=True))
        return 2
    mode = "apply" if args.apply else "status"
    exit_code = 0
    try:
        payload = asyncio.run(_execute(args))
    except KeyboardInterrupt:
        payload = _error(mode, "cancelled")
        exit_code = 130
    except file_storage_relocation.FileStorageCommitAmbiguous:
        payload = _error(mode, "commit_ambiguous")
        exit_code = 1
    except file_storage_relocation.FileStorageRelocationError:
        payload = _error(mode, "relocation_refused")
        exit_code = 2
    except (OSError, ValueError):
        payload = _error(mode, "storage_invalid")
        exit_code = 2
    except Exception:
        payload = _error(mode, "internal_error")
        exit_code = 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
