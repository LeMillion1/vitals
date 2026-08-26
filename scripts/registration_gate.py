#!/usr/bin/env python3
"""Persist and read back the deployment-level account-registration gate.

The runtime file is shared with a running web process, whose Settings writes
use the same atomic editor. To remove cross-process lost-update ambiguity, the
operator must stop web before changing the file and attest that fact with the
exact confirmation phrase printed by ``--help``. A successful write is not
active yet: web must be recreated and health-checked before a non-disabled
stored registration mode is selected.

Production opening sequence::

    docker compose stop vitals_app
    python scripts/registration_gate.py --set unlocked \
        --confirm 'WEB STOPPED; UNLOCK REGISTRATION'
    docker compose up -d --force-recreate vitals_app
    docker compose ps vitals_app
    python scripts/registration_mode.py --set invite_only \
        --runtime-env .vitals-runtime/vitals.env \
        --confirm-web-recreated \
        'WEB RECREATED WITH REGISTRATION GATE ENABLED'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vitals.runtime_env import (  # noqa: E402
    RuntimeEnvIsolationError,
    read_env_key,
    validate_runtime_environment,
    write_env_keys,
)
from vitals.services.authentication.registration import (  # noqa: E402
    REGISTRATION_UNLOCK_ENV,
)


DEFAULT_RUNTIME_ENV = Path(".vitals-runtime/vitals.env")
UNLOCK_CONFIRMATION = "WEB STOPPED; UNLOCK REGISTRATION"
LOCK_CONFIRMATION = "WEB STOPPED; LOCK REGISTRATION"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})


def read_gate_state(path: Path) -> str:
    """Return ``unlocked``/``locked`` after owner-only file validation."""

    raw = read_env_key(
        path,
        REGISTRATION_UNLOCK_ENV,
        require_existing=True,
        require_owner_only=True,
    )
    validate_runtime_environment(path, environ={})
    normalized = raw.strip().casefold()
    if normalized in _TRUTHY:
        return "unlocked"
    if normalized in _FALSY:
        return "locked"
    raise RuntimeEnvIsolationError(
        f"{REGISTRATION_UNLOCK_ENV} has an unsupported boolean value"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-env",
        type=Path,
        default=DEFAULT_RUNTIME_ENV,
        help="owner-only application runtime file (default: %(default)s)",
    )
    parser.add_argument(
        "--set",
        choices=("unlocked", "locked"),
        help="persist a new gate state; omit for a read-only status check",
    )
    parser.add_argument(
        "--confirm",
        help=(
            f"exactly {UNLOCK_CONFIRMATION!r} or {LOCK_CONFIRMATION!r}; "
            "the web service must already be stopped"
        ),
    )
    return parser.parse_args(argv)


def _emit(payload: dict[str, object], *, error: bool = False) -> None:
    print(json.dumps(payload, sort_keys=True), file=sys.stderr if error else sys.stdout)


def _run(args: argparse.Namespace) -> int:
    try:
        previous = read_gate_state(args.runtime_env)
        if args.set is None:
            _emit(
                {
                    "operation": "registration_gate",
                    "readback": previous,
                    "result": "ok",
                    "runtime_env": str(args.runtime_env),
                }
            )
            return 0

        expected_confirmation = (
            UNLOCK_CONFIRMATION if args.set == "unlocked" else LOCK_CONFIRMATION
        )
        if args.confirm != expected_confirmation:
            raise RuntimeEnvIsolationError(
                f"refusing change without exact --confirm {expected_confirmation!r}"
            )
        write_env_keys(
            args.runtime_env,
            {REGISTRATION_UNLOCK_ENV: "1" if args.set == "unlocked" else "0"},
            require_existing=True,
            require_owner_only=True,
        )
        readback = read_gate_state(args.runtime_env)
        if readback != args.set:
            raise RuntimeEnvIsolationError("registration gate readback did not match")
    except (OSError, RuntimeEnvIsolationError, TypeError, ValueError) as exc:
        _emit(
            {
                "operation": "registration_gate",
                "reason": str(exc),
                "result": "error",
            },
            error=True,
        )
        return 2

    _emit(
        {
            "next_action": (
                "recreate and health-check vitals_app before changing the stored mode"
            ),
            "operation": "registration_gate",
            "previous": previous,
            "readback": readback,
            "result": "ok",
            "runtime_env": str(args.runtime_env),
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return _run(_parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
