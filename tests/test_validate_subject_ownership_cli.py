"""Non-PHI operator contract for the fixed Stage-4 validation CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from scripts import validate_subject_ownership as cli


EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)
PHI_SENTINEL = "private-lake-row-value-sentinel"
URL_SENTINEL = "postgresql+asyncpg://secret@private.example/health"
UUID_SENTINEL = "75a70fe0-cb4b-47a7-9e29-0f77fe2f246d"
SAFE_KEYS = {
    "checks_total",
    "completed",
    "format_version",
    "graph_checksum",
    "mode",
    "operation",
    "phase",
    "result",
    "rows_inspected",
    "status",
    "tables_total",
    "validated_constraints",
    "violations_total",
}
ERROR_KEYS = {
    "error_code",
    "format_version",
    "mode",
    "operation",
    "phase",
    "result",
}


@dataclass
class _Result:
    status: str = "completed"
    tables_total: int = 42
    checks_total: int = 210
    rows_inspected: int = 1234
    violations_total: int = 0
    validated_constraints: int = 6
    graph_checksum: str = EMPTY_SHA256

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "phase_key": cli.validation_service.OWNERSHIP_VALIDATION_PHASE,
            "status": self.status,
            "tables_total": self.tables_total,
            "checks_total": self.checks_total,
            "rows_inspected": self.rows_inspected,
            "violations_total": self.violations_total,
            "validated_constraints": self.validated_constraints,
            "graph_checksum": self.graph_checksum,
        }


def _run(monkeypatch, argv, *, result=None, error=None):
    calls: list[str] = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def commit(self):
            calls.append("commit")

        async def rollback(self):
            calls.append("rollback")

    def _factory(_config):
        return _Session

    async def _preflight(_session):
        calls.append("preflight")
        if error is not None:
            raise error
        return result or _Result(status="not_started")

    async def _run_validation(_session):
        calls.append("apply")
        if error is not None:
            raise error
        return result or _Result()

    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "create_session_factory", _factory)
    monkeypatch.setattr(
        cli.validation_service, "preflight_ownership_validation", _preflight
    )
    monkeypatch.setattr(
        cli.validation_service, "run_ownership_validation", _run_validation
    )
    return calls


def test_default_is_read_only_and_projects_only_allowlisted_aggregates(
    monkeypatch, capsys
):
    calls = _run(monkeypatch, [], result=_Result(status="not_started"))
    assert cli.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == SAFE_KEYS
    assert payload["mode"] == "status"
    assert payload["result"] == "ok"
    assert payload["completed"] is False
    assert payload["violations_total"] == 0
    assert "preflight" in calls and "commit" not in calls


def test_apply_records_evidence_and_commits(monkeypatch, capsys):
    calls = _run(monkeypatch, ["--apply"], result=_Result())
    assert cli.main(["--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == SAFE_KEYS
    assert payload["mode"] == "apply"
    assert payload["completed"] is True
    assert payload["validated_constraints"] == 6
    assert "apply" in calls and "commit" in calls


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    [
        (
            cli.validation_service.OwnershipValidationViolation("secret"),
            "violation",
            2,
        ),
        (
            cli.validation_service.OwnershipValidationDependencyError("secret"),
            "dependency_error",
            1,
        ),
        (
            cli.validation_service.OwnershipValidationIdentityError("secret"),
            "identity_error",
            1,
        ),
        (
            cli.validation_service.OwnershipValidationStateError("secret"),
            "state_error",
            1,
        ),
        (RuntimeError(PHI_SENTINEL), "internal_error", 1),
    ],
)
def test_typed_errors_are_projected_without_detail(
    monkeypatch, capsys, error, code, exit_code
):
    _run(monkeypatch, [], error=error)
    assert cli.main([]) == exit_code
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    assert set(payload) == ERROR_KEYS
    assert payload["error_code"] == code
    assert PHI_SENTINEL not in rendered
    assert "secret" not in rendered


@pytest.mark.parametrize(
    "argv",
    [["--table", "weight_logs"], ["--phase", "x"], ["--reset"], ["--database-url", URL_SENTINEL]],
)
def test_no_table_phase_reset_or_database_selector_exists(monkeypatch, capsys, argv):
    assert cli.main(argv) == 2
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    assert set(payload) == ERROR_KEYS
    assert payload["error_code"] == "invalid_arguments"
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    "unsafe",
    [
        {"violations_total": 1},
        {"graph_checksum": UUID_SENTINEL},
        {"tables_total": 0},
        {"checks_total": 1},
    ],
)
def test_unsafe_projections_are_refused(monkeypatch, capsys, unsafe):
    result = _Result(**unsafe)
    _run(monkeypatch, [], result=result)
    assert cli.main([]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "error"
    assert payload["error_code"] == "internal_error"


def test_phase_and_operation_are_fixed():
    assert cli.OPERATION == "subject_ownership_validation"
    assert cli.validation_service.OWNERSHIP_VALIDATION_PHASE == (
        "stage4.whole_lake_validation.v1"
    )
