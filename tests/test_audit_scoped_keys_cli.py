"""Non-PHI operator contract for the fixed Stage-5A scoped-key audit CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from scripts import audit_scoped_keys as cli


EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)
PHI_SENTINEL = "private-lake-key-value-sentinel"
URL_SENTINEL = "postgresql+asyncpg://secret@private.example/health"
UUID_SENTINEL = "75a70fe0-cb4b-47a7-9e29-0f77fe2f246d"
SAFE_KEYS = {
    "audit_checksum",
    "collisions_total",
    "completed",
    "format_version",
    "legacy_keys_total",
    "mode",
    "operation",
    "phase",
    "result",
    "rows_inspected",
    "scoped_indexes_total",
    "status",
    "unscoped_rows_total",
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
    legacy_keys_total: int = 12
    scoped_indexes_total: int = 16
    rows_inspected: int = 1234
    collisions_total: int = 0
    unscoped_rows_total: int = 0
    audit_checksum: str = EMPTY_SHA256

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "phase_key": cli.audit_service.SCOPED_KEY_AUDIT_PHASE,
            "status": self.status,
            "legacy_keys_total": self.legacy_keys_total,
            "scoped_indexes_total": self.scoped_indexes_total,
            "rows_inspected": self.rows_inspected,
            "collisions_total": self.collisions_total,
            "unscoped_rows_total": self.unscoped_rows_total,
            "audit_checksum": self.audit_checksum,
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
        cli.audit_service, "preflight_scoped_key_audit", _preflight
    )
    monkeypatch.setattr(cli.audit_service, "run_scoped_key_audit", _run_validation)
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
    assert payload["collisions_total"] == 0
    assert "preflight" in calls and "commit" not in calls


def test_apply_records_evidence_and_commits(monkeypatch, capsys):
    calls = _run(monkeypatch, ["--apply"], result=_Result())
    assert cli.main(["--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == SAFE_KEYS
    assert payload["mode"] == "apply"
    assert payload["completed"] is True
    assert payload["scoped_indexes_total"] == 16
    assert "apply" in calls and "commit" in calls


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    [
        (
            cli.audit_service.ScopedKeyAuditCollision("secret"),
            "collision",
            2,
        ),
        (
            cli.audit_service.ScopedKeyAuditDependencyError("secret"),
            "dependency_error",
            1,
        ),
        (
            cli.audit_service.ScopedKeyAuditStateError("secret"),
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
    [
        ["--table", "weight_logs"],
        ["--key", "uq_active_weight_per_date"],
        ["--reset"],
        ["--database-url", URL_SENTINEL],
    ],
)
def test_no_table_key_reset_or_database_selector_exists(monkeypatch, capsys, argv):
    assert cli.main(argv) == 2
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    assert set(payload) == ERROR_KEYS
    assert payload["error_code"] == "invalid_arguments"
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    "unsafe",
    [
        {"collisions_total": 1},
        {"unscoped_rows_total": 1},
        {"audit_checksum": UUID_SENTINEL},
        {"legacy_keys_total": 0},
        {"scoped_indexes_total": 1},
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
    assert cli.OPERATION == "scoped_key_audit"
    assert cli.audit_service.SCOPED_KEY_AUDIT_PHASE == "stage5.scoped_key_audit.v1"
