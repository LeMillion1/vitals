"""Non-PHI operator contract for the Stage-3B normalized backfill CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts import backfill_normalized_subject_ownership as cli


EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)
PHI_SENTINEL = "private-health-payload-sentinel"
URL_SENTINEL = "postgresql+asyncpg://secret@private.example/health"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CATALOG = (
    "hrt_cycles",
    "hrt_cycle_templates",
    "annotations",
    "body_measurements",
    "glp1_dose_phases",
    "glp1_injections",
    "glp1_side_effects",
    "hrt_doses",
    "hrt_side_effects",
    "lab_markers",
    "meal_logs",
    "milestones",
    "noise_markers",
    "skincare_logs",
    "skincare_observations",
    "skincare_products",
    "supplements",
)


@dataclass
class _Result:
    status: str = "running"
    tables_total: int = len(CATALOG)
    completed_tables: int = 1
    snapshot_rows: int = 5
    scanned_rows: int = 2
    updated_rows: int = 2
    unchanged_rows: int = 0
    remaining_rows: int = 3
    rows_above_high_watermark: int = 0
    batch_table: str | None = None
    batch_scanned_rows: int | None = None
    batch_updated_rows: int | None = None
    batch_unchanged_rows: int | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phase_key": cli.backfill_service.NORMALIZED_MANUAL_BACKFILL_PHASE,
            "status": self.status,
            "tables_total": self.tables_total,
            "completed_tables": self.completed_tables,
            "snapshot_rows": self.snapshot_rows,
            "scanned_rows": self.scanned_rows,
            "updated_rows": self.updated_rows,
            "unchanged_rows": self.unchanged_rows,
            "remaining_rows": self.remaining_rows,
            "rows_above_high_watermark": self.rows_above_high_watermark,
            "data_checksum_before": EMPTY_SHA256,
            "data_checksum_after": EMPTY_SHA256,
            "ownership_checksum_after": EMPTY_SHA256,
            # Service internals and arbitrary extras must not cross the stricter
            # CLI boundary even if a future result accidentally exposes them.
            "subject_id": PHI_SENTINEL,
            "last_scanned_id": 12345,
            "scan_high_watermark_id": 98765,
            "payload": PHI_SENTINEL,
            "created_at": PHI_SENTINEL,
            "error": PHI_SENTINEL,
        }
        if self.batch_table is not None:
            result.update(
                {
                    "batch_table": self.batch_table,
                    "batch_scanned_rows": self.batch_scanned_rows,
                    "batch_updated_rows": self.batch_updated_rows,
                    "batch_unchanged_rows": self.batch_unchanged_rows,
                }
            )
        return result


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _Factory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []
        self.kw: dict[str, Any] = {}
        self.session_type: type[_Session] = _Session

    def __call__(self) -> _Session:
        session = self.session_type()
        self.sessions.append(session)
        return session


def _install_factory(monkeypatch) -> _Factory:
    factory = _Factory()
    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "create_session_factory", lambda _config: factory)
    monkeypatch.setattr(cli, "_catalog_tables", lambda: CATALOG)
    return factory


def _output(capsys) -> tuple[dict[str, Any], str]:
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.out + captured.err


def test_direct_script_invocation_resolves_package_and_uses_one_json_format():
    process = subprocess.run(
        [
            sys.executable,
            str(
                REPOSITORY_ROOT
                / "scripts"
                / "backfill_normalized_subject_ownership.py"
            ),
            "--help",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert process.returncode == 2
    assert process.stderr == ""
    assert process.stdout.count("\n") == 1
    assert json.loads(process.stdout) == {
        "error_code": "invalid_arguments",
        "format_version": 1,
        "mode": "argument",
        "operation": "normalized_subject_ownership_backfill",
        "phase": "stage3.normalized_manual.v1",
        "result": "error",
    }


def test_default_is_read_only_and_applies_a_second_fixed_allowlist(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)
    calls = {"status": 0, "apply": 0}

    async def preflight(_session):
        calls["status"] += 1
        return _Result()

    async def apply(_session, *, batch_size):
        del batch_size
        calls["apply"] += 1
        raise AssertionError("default status must not enter the mutator")

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_normalized_ownership_backfill",
        preflight,
    )
    monkeypatch.setattr(
        cli.backfill_service,
        "run_normalized_ownership_backfill_batch",
        apply,
    )

    assert cli.main([]) == 0
    payload, rendered = _output(capsys)

    assert calls == {"status": 1, "apply": 0}
    assert len(factory.sessions) == 1
    assert factory.sessions[0].rollbacks == 1
    assert factory.sessions[0].commits == 0
    assert payload == {
        "batch_scanned_rows": 0,
        "batch_size": 250,
        "batch_table": None,
        "batch_unchanged_rows": 0,
        "batch_updated_rows": 0,
        "batches_processed": 0,
        "completed": False,
        "completed_tables": 1,
        "data_checksum_after": EMPTY_SHA256,
        "data_checksum_before": EMPTY_SHA256,
        "format_version": 1,
        "max_batches": 1,
        "mode": "status",
        "operation": "normalized_subject_ownership_backfill",
        "ownership_checksum_after": EMPTY_SHA256,
        "phase": "stage3.normalized_manual.v1",
        "remaining_rows": 3,
        "result": "ok",
        "rows_above_high_watermark": 0,
        "scanned_rows": 2,
        "snapshot_rows": 5,
        "status": "running",
        "tables_total": len(CATALOG),
        "unchanged_rows": 0,
        "updated_rows": 2,
    }
    assert set(payload).isdisjoint(
        {
            "subject_id",
            "last_scanned_id",
            "scan_high_watermark_id",
            "payload",
            "created_at",
            "updated_at",
            "error",
            "argv",
            "database_url",
        }
    )
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_apply_commits_each_batch_in_a_fresh_session_and_stops_at_completion(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)
    results = iter(
        (
            _Result(
                completed_tables=1,
                batch_table="annotations",
                batch_scanned_rows=2,
                batch_updated_rows=2,
                batch_unchanged_rows=0,
            ),
            _Result(
                status="completed",
                completed_tables=len(CATALOG),
                scanned_rows=5,
                updated_rows=4,
                unchanged_rows=1,
                remaining_rows=0,
                batch_table="supplements",
                batch_scanned_rows=3,
                batch_updated_rows=2,
                batch_unchanged_rows=1,
            ),
        )
    )
    batch_sizes: list[int] = []

    async def apply(_session, *, batch_size):
        batch_sizes.append(batch_size)
        return next(results)

    monkeypatch.setattr(
        cli.backfill_service,
        "run_normalized_ownership_backfill_batch",
        apply,
    )

    assert cli.main(["--apply", "--batch-size", "2", "--max-batches", "9"]) == 0
    payload, _rendered = _output(capsys)

    assert batch_sizes == [2, 2]
    assert len(factory.sessions) == 2
    assert [session.commits for session in factory.sessions] == [1, 1]
    assert payload["status"] == "completed"
    assert payload["completed"] is True
    assert payload["batch_table"] == "supplements"
    assert payload["batches_processed"] == 2
    assert payload["completed_tables"] == len(CATALOG)


def test_apply_stops_at_bounded_max_batches_while_group_is_running(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)
    calls = 0

    async def apply(_session, *, batch_size):
        nonlocal calls
        assert batch_size == 7
        calls += 1
        return _Result(
            completed_tables=calls,
            snapshot_rows=calls * 7 + 50,
            scanned_rows=calls * 7,
            updated_rows=calls * 7,
            remaining_rows=50,
            batch_table=CATALOG[calls - 1],
            batch_scanned_rows=7,
            batch_updated_rows=7,
            batch_unchanged_rows=0,
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "run_normalized_ownership_backfill_batch",
        apply,
    )

    assert cli.main(["--apply", "--batch-size", "7", "--max-batches", "3"]) == 0
    payload, _rendered = _output(capsys)
    assert calls == 3
    assert len(factory.sessions) == 3
    assert all(session.commits == 1 for session in factory.sessions)
    assert payload["status"] == "running"
    assert payload["batches_processed"] == 3


@pytest.mark.parametrize(
    "argv",
    [
        ["--batch-size", "0"],
        ["--batch-size", "-1"],
        ["--batch-size", "1001"],
        ["--max-batches", "0"],
        ["--max-batches", "-1"],
        ["--max-batches", "101"],
        ["--phase", "stage3.normalized_manual.v1"],
        ["--table", "annotations"],
        ["--database-url", URL_SENTINEL],
        ["--app"],
        ["--batch", "2"],
        ["--reset"],
        ["--delete"],
        ["annotations"],
    ],
)
def test_invalid_bounds_and_unreviewed_targets_are_rejected_before_config(
    monkeypatch,
    capsys,
    argv,
):
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("must not load config")),
    )
    assert cli.main(argv) == 2
    payload, rendered = _output(capsys)
    assert payload == {
        "error_code": "invalid_arguments",
        "format_version": 1,
        "mode": "argument",
        "operation": "normalized_subject_ownership_backfill",
        "phase": "stage3.normalized_manual.v1",
        "result": "error",
    }
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    ("error_name", "expected_code", "expected_exit"),
    [
        ("NormalizedOwnershipBackfillValidationError", "validation_error", 2),
        ("NormalizedOwnershipBackfillIdentityError", "identity_error", 1),
        ("NormalizedOwnershipBackfillDependencyError", "dependency_error", 1),
        ("NormalizedOwnershipBackfillStateError", "state_error", 1),
        ("NormalizedOwnershipBackfillProvenanceError", "provenance_error", 1),
    ],
)
def test_typed_failures_emit_only_bounded_codes(
    monkeypatch,
    capsys,
    error_name,
    expected_code,
    expected_exit,
):
    _install_factory(monkeypatch)
    error_type = getattr(cli.backfill_service, error_name)

    async def fail(_session):
        raise error_type(f"{PHI_SENTINEL} {URL_SENTINEL}")

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_normalized_ownership_backfill",
        fail,
    )

    assert cli.main([]) == expected_exit
    payload, rendered = _output(capsys)
    assert payload == {
        "error_code": expected_code,
        "format_version": 1,
        "mode": "status",
        "operation": "normalized_subject_ownership_backfill",
        "phase": "stage3.normalized_manual.v1",
        "result": "error",
    }
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    ("unsafe_key", "unsafe_value"),
    [
        ("status", PHI_SENTINEL),
        ("batch_table", PHI_SENTINEL),
        ("tables_total", len(CATALOG) - 1),
        ("updated_rows", True),
        ("completed_tables", len(CATALOG) + 1),
        ("data_checksum_after", PHI_SENTINEL),
    ],
)
def test_unsafe_service_projection_is_sanitized(
    monkeypatch,
    capsys,
    unsafe_key,
    unsafe_value,
):
    _install_factory(monkeypatch)

    async def unsafe(_session):
        result = _Result(batch_table="annotations", batch_scanned_rows=1)
        safe = result.to_safe_dict()
        safe[unsafe_key] = unsafe_value
        result.to_safe_dict = lambda: safe
        return result

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_normalized_ownership_backfill",
        unsafe,
    )
    assert cli.main([]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "internal_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_unexpected_cancel_and_commit_failure_never_expose_details(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)

    async def fail(_session):
        raise RuntimeError(f"{PHI_SENTINEL} {URL_SENTINEL}")

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_normalized_ownership_backfill",
        fail,
    )
    assert cli.main([]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "internal_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered

    async def cancelled(_session):
        raise asyncio.CancelledError(f"{PHI_SENTINEL} {URL_SENTINEL}")

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_normalized_ownership_backfill",
        cancelled,
    )
    assert cli.main([]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "cancelled"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered

    class _FailingCommitSession(_Session):
        async def commit(self) -> None:
            raise RuntimeError(f"{PHI_SENTINEL} {URL_SENTINEL}")

    factory.session_type = _FailingCommitSession

    async def apply(_session, *, batch_size):
        assert batch_size == 2
        return _Result(
            batch_table="annotations",
            batch_scanned_rows=1,
            batch_updated_rows=1,
            batch_unchanged_rows=0,
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "run_normalized_ownership_backfill_batch",
        apply,
    )
    assert cli.main(["--apply", "--batch-size", "2"]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "internal_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered
