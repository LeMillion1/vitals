#!/usr/bin/env python3
"""Create an application-only env file in a dedicated host directory.

The command is intentionally one-shot: it refuses to replace an existing
runtime file because Settings writes that file after deployment and the host
operator file is not authoritative for those later changes.  An explicit
legacy migration mode copies only validated assignments from ``.env.runtime``;
it leaves that old file intact for the reviewed pre-split emergency rollback.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vitals.runtime_env import (
    RUNTIME_ENV_KEYS,
    RuntimeEnvIsolationError,
    parse_assignment_lines,
    validate_runtime_environment,
)

DEFAULT_DESTINATION = Path(".vitals-runtime/vitals.env")


def _write_new_runtime_environment(
    *, destination: Path, assignments: list[tuple[str, str]], header: str
) -> int:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise RuntimeEnvIsolationError(
            "application runtime environment parent must be a real directory"
        )
    directory_stat = destination.parent.stat()
    if directory_stat.st_uid != os.geteuid():
        raise RuntimeEnvIsolationError(
            "application runtime environment directory must belong to the current user"
        )
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise RuntimeEnvIsolationError(
            "application runtime environment directory must have mode 0700"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeEnvIsolationError(
            "application runtime environment already exists; refusing to overwrite it"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(header)
            for _key, line in assignments:
                stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(destination, 0o600)
        validate_runtime_environment(destination, environ={})
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(destination.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return len(assignments)


def create_runtime_env(*, source: Path, destination: Path) -> int:
    if source.resolve(strict=False) == destination.resolve(strict=False):
        raise RuntimeEnvIsolationError(
            "source and application runtime environment must be different files"
        )
    assignments = parse_assignment_lines(source)
    selected = [(key, line) for key, line in assignments if key in RUNTIME_ENV_KEYS]
    selected_keys = {key for key, _line in selected}
    if "VITALS_DATABASE_URL" not in selected_keys:
        raise RuntimeEnvIsolationError("source is missing VITALS_DATABASE_URL")
    return _write_new_runtime_environment(
        destination=destination,
        assignments=selected,
        header=(
            "# Application-only configuration. Operator and DB-owner secrets "
            "must stay in .env.\n"
        ),
    )


def migrate_runtime_env(*, source: Path, destination: Path) -> int:
    """Copy one validated legacy runtime file without trusting operator ``.env``."""

    if source.resolve(strict=False) == destination.resolve(strict=False):
        raise RuntimeEnvIsolationError(
            "legacy and application runtime environments must be different files"
        )
    validate_runtime_environment(source, environ={})
    assignments = parse_assignment_lines(source)
    return _write_new_runtime_environment(
        destination=destination,
        assignments=assignments,
        header=(
            "# Application-only configuration migrated from the legacy runtime "
            "file. Operator and DB-owner secrets must stay in .env.\n"
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--source", type=Path, default=Path(".env"))
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    sources.add_argument(
        "--migrate-from",
        type=Path,
        help="copy a validated legacy runtime file instead of filtering --source",
    )
    args = parser.parse_args()
    try:
        if args.migrate_from is None:
            keys_written = create_runtime_env(
                source=args.source,
                destination=args.destination,
            )
            mode = "created"
        else:
            keys_written = migrate_runtime_env(
                source=args.migrate_from,
                destination=args.destination,
            )
            mode = "migrated"
    except RuntimeEnvIsolationError as exc:
        print(
            json.dumps(
                {"operation": "create_runtime_env", "result": "error", "reason": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "destination": str(args.destination),
                "keys_written": keys_written,
                "mode": mode,
                "operation": "create_runtime_env",
                "result": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
