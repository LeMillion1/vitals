#!/usr/bin/env python3
"""Create an application-only env file from the host operator env.

The command is intentionally one-shot: it refuses to replace an existing
runtime file because Settings writes that file after deployment and the host
operator file is not authoritative for those later changes.
"""

from __future__ import annotations

import argparse
import json
import os
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

    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeEnvIsolationError(
            "application runtime environment already exists; refusing to overwrite it"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(
                "# Application-only configuration. Operator and DB-owner secrets "
                "must stay in .env.\n"
            )
            for _key, line in selected:
                stream.write(line)
        os.chmod(destination, 0o600)
        validate_runtime_environment(destination, environ={})
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return len(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(".env"))
    parser.add_argument("--destination", type=Path, default=Path(".env.runtime"))
    args = parser.parse_args()
    try:
        keys_written = create_runtime_env(
            source=args.source,
            destination=args.destination,
        )
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
                "operation": "create_runtime_env",
                "result": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
