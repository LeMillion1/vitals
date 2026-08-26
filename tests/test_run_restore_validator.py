"""Aggregate-only envelope tests for restore validators."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "vitals_run_restore_validator", ROOT / "scripts/run_restore_validator.py"
)
assert SPEC is not None and SPEC.loader is not None
wrapper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wrapper
SPEC.loader.exec_module(wrapper)


def test_runtime_error_preserves_only_bounded_code():
    output = json.dumps(
        {
            "error_code": "subject_data_missing",
            "operation": "validate_runtime_rls",
            "result": "error",
        }
    )

    assert wrapper._sanitize_runtime_payload(output) == {
        "error_code": "subject_data_missing",
        "operation": "restore_validator_runner",
        "result": "error",
    }


def test_runtime_success_preserves_only_aggregate_counts():
    payload = {
        "bound_visible_rows": 7,
        "forced_rls_tables": 56,
        "inspected_subject_rows": 7,
        "operation": "validate_runtime_rls",
        "platform_visible_rows": 7,
        "required_subject_tables": 48,
        "result": "ok",
        "subjects": 1,
        "unbound_visible_rows": 0,
        "validated_subjects": 1,
    }

    safe = wrapper._sanitize_runtime_payload(json.dumps(payload))

    assert safe == payload | {"operation": "restore_validator_runner"}


def test_runtime_output_rejects_extra_fields_and_unbounded_errors():
    assert wrapper._sanitize_runtime_payload(
        '{"operation":"validate_runtime_rls","result":"error",'
        '"error_code":"dsn=secret"}'
    ) == {
        "error_code": "validator_output_invalid",
        "operation": "restore_validator_runner",
        "result": "error",
    }
    assert wrapper._sanitize_runtime_payload(
        '{"operation":"validate_runtime_rls","result":"ok",'
        '"database_url":"secret"}'
    )["error_code"] == "validator_output_invalid"


def test_runtime_output_rejects_missing_or_invalid_counts():
    assert wrapper._sanitize_runtime_payload("traceback only")["error_code"] == (
        "validator_output_invalid"
    )
    payload = {
        "bound_visible_rows": -1,
        "forced_rls_tables": 1,
        "inspected_subject_rows": 1,
        "operation": "validate_runtime_rls",
        "required_subject_tables": 1,
        "result": "ok",
        "subjects": 1,
        "unbound_visible_rows": 0,
        "validated_subjects": 1,
    }
    assert wrapper._sanitize_runtime_payload(json.dumps(payload))["error_code"] == (
        "validator_output_invalid"
    )
