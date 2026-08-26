#!/usr/bin/env python3
"""Run one allowlisted restore validator with a strict aggregate-only envelope."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
import re
import sys
from typing import Any


OPERATION = "restore_validator_runner"
_RUNTIME_OPERATION = "validate_runtime_rls"
_RUNTIME_COUNT_KEYS = frozenset(
    {
        "bound_visible_rows",
        "forced_rls_tables",
        "inspected_subject_rows",
        "required_subject_tables",
        "subjects",
        "unbound_visible_rows",
        "validated_subjects",
    }
)


def _error(code: str) -> dict[str, str]:
    if re.fullmatch(r"[a-z0-9_]+", code) is None:
        code = "validator_output_invalid"
    return {"error_code": code, "operation": OPERATION, "result": "error"}


def _sanitize_runtime_payload(output: str) -> dict[str, Any]:
    payload: Any = None
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        break
    if not isinstance(payload, dict):
        return _error("validator_output_invalid")
    if payload.get("operation") != _RUNTIME_OPERATION:
        return _error("validator_output_invalid")
    if payload.get("result") == "error":
        if set(payload) != {"error_code", "operation", "result"}:
            return _error("validator_output_invalid")
        code = payload.get("error_code")
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]+", code) is None:
            return _error("validator_output_invalid")
        return _error(code)
    expected = _RUNTIME_COUNT_KEYS | {"operation", "result"}
    if payload.get("result") != "ok" or set(payload) != expected:
        return _error("validator_output_invalid")
    safe: dict[str, Any] = {"operation": OPERATION, "result": "ok"}
    for key in sorted(_RUNTIME_COUNT_KEYS):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return _error("validator_output_invalid")
        safe[key] = value
    return safe


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments != ["runtime-rls"]:
        payload = _error("invalid_arguments")
        exit_code = 2
    else:
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                validator = importlib.import_module("validate_runtime_rls")
                validator_exit = validator.main([])
        except BaseException:
            payload = _error("validator_crashed")
            exit_code = 1
        else:
            payload = _sanitize_runtime_payload(stdout.getvalue())
            result_ok = payload.get("result") == "ok"
            exit_code = 0 if result_ok and validator_exit == 0 else 1
            if result_ok and validator_exit != 0:
                payload = _error("validator_exit_mismatch")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
