"""Non-PHI operator contract for the fixed Stage-3H progress-photo CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts import backfill_progress_photo_subject_ownership as cli


EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)
PHI_SENTINEL = "private-progress-photo-health-sentinel"
URL_SENTINEL = "postgresql+asyncpg://secret@private.example/health"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CATALOG = ("progress_photos",)
SAFE_KEYS = {
    "batch_scanned_rows",
    "batch_size",
    "batch_table",
    "batch_unchanged_rows",
    "batch_updated_rows",
    "batches_processed",
    "completed",
    "completed_tables",
    "data_checksum_after",
    "data_checksum_before",
    "format_version",
    "max_batches",
    "mode",
    "operation",
    "ownership_checksum_after",
    "phase",
    "remaining_rows",
    "result",
    "rows_above_high_watermark",
    "scanned_rows",
    "snapshot_rows",
    "status",
    "tables_total",
    "unchanged_rows",
    "updated_rows",
}


@dataclass
class _Result:
    status: str = "running"
    tables_total: int = len(CATALOG)
    completed_tables: int = 0
    snapshot_rows: int = 4
    scanned_rows: int = 1
    updated_rows: int = 1
    unchanged_rows: int = 0
    remaining_rows: int = 3
    rows_above_high_watermark: int = 0
    batch_table: str | None = None
    batch_scanned_rows: int | None = None
    batch_updated_rows: int | None = None
    batch_unchanged_rows: int | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phase_key": cli.backfill_service.PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
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
            "subject_id": PHI_SENTINEL,
            "last_scanned_id": 12345,
            "scan_high_watermark_id": 98765,
            "file_key": PHI_SENTINEL,
            "file_asset_id": PHI_SENTINEL,
            "actor_user_id": PHI_SENTINEL,
            "uploaded_by_user_id": PHI_SENTINEL,
            "date": PHI_SENTINEL,
            "note": PHI_SENTINEL,
            "errors": [f"{PHI_SENTINEL} {URL_SENTINEL}"],
            "database_url": URL_SENTINEL,
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
    return factory


def _output(capsys) -> tuple[dict[str, Any], str]:
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.out + captured.err


def test_direct_invocation_is_fixed_target_and_one_safe_json_line():
    process = subprocess.run(
        [
            sys.executable,
            str(
                REPOSITORY_ROOT
                / "scripts"
                / "backfill_progress_photo_subject_ownership.py"
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
        "operation": "progress_photo_subject_ownership_backfill",
        "phase": cli.backfill_service.PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
        "result": "error",
    }


def test_default_is_read_only_and_projects_only_allowlisted_counts(
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
        "preflight_progress_photo_ownership_backfill",
        preflight,
    )
    monkeypatch.setattr(
        cli.backfill_service,
        "run_progress_photo_ownership_backfill_batch",
        apply,
    )

    assert cli.main([]) == 0
    payload, rendered = _output(capsys)
    assert calls == {"status": 1, "apply": 0}
    assert len(factory.sessions) == 1
    assert factory.sessions[0].rollbacks == 1
    assert factory.sessions[0].commits == 0
    assert set(payload) == SAFE_KEYS
    assert payload["operation"] == "progress_photo_subject_ownership_backfill"
    assert payload["mode"] == "status"
    assert payload["batches_processed"] == 0
    assert payload["batch_table"] is None
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    ("status", "completed", "snapshot", "scanned", "remaining", "completed_tables"),
    [
        ("not_started", False, 4, 0, 4, 0),
        ("running", False, 4, 1, 3, 0),
        ("completed", True, 4, 4, 0, 1),
        ("restore_blocked", False, 4, 0, 4, 0),
    ],
)
def test_exact_status_catalog_is_projected_safely(
    monkeypatch,
    capsys,
    status,
    completed,
    snapshot,
    scanned,
    remaining,
    completed_tables,
):
    _install_factory(monkeypatch)

    async def preflight(_session):
        return _Result(
            status=status,
            completed_tables=completed_tables,
            snapshot_rows=snapshot,
            scanned_rows=scanned,
            updated_rows=scanned,
            remaining_rows=remaining,
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_progress_photo_ownership_backfill",
        preflight,
    )
    assert cli.main([]) == 0
    payload, _rendered = _output(capsys)
    assert payload["status"] == status
    assert payload["completed"] is completed


def test_apply_uses_fresh_committed_sessions_and_stops_at_completion(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)
    results = iter(
        (
            _Result(
                snapshot_rows=2,
                scanned_rows=1,
                remaining_rows=1,
                batch_table="progress_photos",
                batch_scanned_rows=1,
                batch_updated_rows=1,
                batch_unchanged_rows=0,
            ),
            _Result(
                status="completed",
                completed_tables=1,
                snapshot_rows=2,
                scanned_rows=2,
                updated_rows=2,
                remaining_rows=0,
                batch_table="progress_photos",
                batch_scanned_rows=1,
                batch_updated_rows=1,
                batch_unchanged_rows=0,
            ),
        )
    )
    batch_sizes: list[int] = []

    async def apply(_session, *, batch_size):
        batch_sizes.append(batch_size)
        return next(results)

    monkeypatch.setattr(
        cli.backfill_service,
        "run_progress_photo_ownership_backfill_batch",
        apply,
    )
    assert cli.main(["--apply", "--batch-size", "1", "--max-batches", "9"]) == 0
    payload, _rendered = _output(capsys)
    assert batch_sizes == [1, 1]
    assert len(factory.sessions) == 2
    assert [session.commits for session in factory.sessions] == [1, 1]
    assert payload["completed"] is True
    assert payload["batches_processed"] == 2


def test_apply_stops_at_bounded_max_batches(monkeypatch, capsys):
    factory = _install_factory(monkeypatch)
    calls = 0

    async def apply(_session, *, batch_size):
        nonlocal calls
        assert batch_size == 1
        calls += 1
        return _Result(
            snapshot_rows=10,
            scanned_rows=calls,
            updated_rows=calls,
            remaining_rows=10 - calls,
            batch_table="progress_photos",
            batch_scanned_rows=1,
            batch_updated_rows=1,
            batch_unchanged_rows=0,
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "run_progress_photo_ownership_backfill_batch",
        apply,
    )
    assert cli.main(["--apply", "--batch-size", "1", "--max-batches", "3"]) == 0
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
        ["--batch-size", "1001"],
        ["--max-batches", "0"],
        ["--max-batches", "101"],
        ["--phase", "stage3.file_backed.progress_photos.v1"],
        ["--table", "progress_photos"],
        ["--database-url", URL_SENTINEL],
        ["--app"],
        ["--batch", "2"],
        ["--reset"],
        ["--delete"],
        ["progress_photos"],
    ],
)
def test_invalid_bounds_and_targets_fail_before_config(monkeypatch, capsys, argv):
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("must not load config")),
    )
    assert cli.main(argv) == 2
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "invalid_arguments"
    assert payload["mode"] == "argument"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    ("error_name", "expected_code", "expected_exit"),
    [
        ("ProgressPhotoOwnershipBackfillValidationError", "validation_error", 2),
        ("ProgressPhotoOwnershipBackfillIdentityError", "identity_error", 1),
        ("ProgressPhotoOwnershipBackfillDependencyError", "dependency_error", 1),
        ("ProgressPhotoOwnershipBackfillStateError", "state_error", 1),
        ("ProgressPhotoOwnershipBackfillProvenanceError", "provenance_error", 1),
        ("ProgressPhotoOwnershipBackfillDuplicateError", "duplicate_error", 1),
    ],
)
def test_typed_failures_emit_bounded_codes(
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
        "preflight_progress_photo_ownership_backfill",
        fail,
    )
    assert cli.main([]) == expected_exit
    payload, rendered = _output(capsys)
    assert payload["error_code"] == expected_code
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_restore_blocked_apply_emits_only_typed_error_code(monkeypatch, capsys):
    _install_factory(monkeypatch)

    async def fail(_session, *, batch_size):
        assert batch_size == 1
        raise cli.backfill_service.ProgressPhotoOwnershipBackfillStateError(
            f"{PHI_SENTINEL} {URL_SENTINEL}"
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "run_progress_photo_ownership_backfill_batch",
        fail,
    )
    assert cli.main(["--apply", "--batch-size", "1"]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "state_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    ("unsafe_key", "unsafe_value"),
    [
        ("status", PHI_SENTINEL),
        ("batch_table", PHI_SENTINEL),
        ("tables_total", 2),
        ("updated_rows", True),
        ("completed_tables", 2),
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
        result = _Result(
            batch_table="progress_photos",
            batch_scanned_rows=1,
            batch_updated_rows=1,
            batch_unchanged_rows=0,
        )
        safe = result.to_safe_dict()
        safe[unsafe_key] = unsafe_value
        result.to_safe_dict = lambda: safe
        return result

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_progress_photo_ownership_backfill",
        unsafe,
    )
    assert cli.main([]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "internal_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_cancel_and_commit_failure_never_expose_details(monkeypatch, capsys):
    factory = _install_factory(monkeypatch)

    async def cancelled(_session):
        raise asyncio.CancelledError(f"{PHI_SENTINEL} {URL_SENTINEL}")

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_progress_photo_ownership_backfill",
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
            batch_table="progress_photos",
            batch_scanned_rows=1,
            batch_updated_rows=1,
            batch_unchanged_rows=0,
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "run_progress_photo_ownership_backfill_batch",
        apply,
    )
    assert cli.main(["--apply", "--batch-size", "2"]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "internal_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered
