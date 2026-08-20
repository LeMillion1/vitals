"""Non-PHI operator contract for the Stage-3A ownership-backfill CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts import backfill_subject_ownership as cli


EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)
PHI_SENTINEL = "private-health-payload-sentinel"
URL_SENTINEL = "postgresql+asyncpg://secret@private.example/health"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class _Result:
    status: str = "running"
    scanned_rows: int = 2
    updated_rows: int = 2
    unchanged_rows: int = 0
    remaining_rows: int = 3
    rows_above_high_watermark: int = 0
    batch_scanned_rows: int | None = None
    batch_updated_rows: int | None = None
    batch_unchanged_rows: int | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phase_key": cli.backfill_service.RAW_OWNERSHIP_BACKFILL_PHASE,
            "status": self.status,
            # The service keeps these internal resume coordinates.  The CLI
            # must apply its own narrower projection and never serialize them.
            "scan_high_watermark_id": 98_765,
            "last_scanned_id": 12_345,
            "scanned_rows": self.scanned_rows,
            "updated_rows": self.updated_rows,
            "unchanged_rows": self.unchanged_rows,
            "remaining_rows": self.remaining_rows,
            "rows_above_high_watermark": self.rows_above_high_watermark,
            "data_checksum_before": EMPTY_SHA256,
            "data_checksum_after": EMPTY_SHA256,
            "ownership_checksum_after": EMPTY_SHA256,
            "subject_id": PHI_SENTINEL,
            "payload": PHI_SENTINEL,
        }
        if self.batch_scanned_rows is not None:
            result.update(
                {
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


def test_direct_script_invocation_resolves_package_and_keeps_one_json_format():
    process = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "backfill_subject_ownership.py"),
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
        "operation": "subject_ownership_backfill",
        "phase": "stage3.raw_payloads.v1",
        "result": "error",
    }


def test_default_is_read_only_status_and_cli_applies_a_second_no_id_allowlist(
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
        raise AssertionError("default status must never enter the mutator")

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_raw_ownership_backfill",
        preflight,
    )
    monkeypatch.setattr(
        cli.backfill_service,
        "run_raw_ownership_backfill_batch",
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
        "batch_unchanged_rows": 0,
        "batch_updated_rows": 0,
        "batches_processed": 0,
        "completed": False,
        "data_checksum_after": EMPTY_SHA256,
        "data_checksum_before": EMPTY_SHA256,
        "format_version": 1,
        "max_batches": 1,
        "mode": "status",
        "operation": "subject_ownership_backfill",
        "ownership_checksum_after": EMPTY_SHA256,
        "phase": "stage3.raw_payloads.v1",
        "remaining_rows": 3,
        "result": "ok",
        "rows_above_high_watermark": 0,
        "scanned_rows": 2,
        "status": "running",
        "unchanged_rows": 0,
        "updated_rows": 2,
    }
    assert set(payload).isdisjoint(
        {
            "subject_id",
            "scan_high_watermark_id",
            "last_scanned_id",
            "payload",
        }
    )
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_apply_commits_each_batch_separately_and_stops_when_completed(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)
    results = iter(
        (
            _Result(
                status="running",
                scanned_rows=2,
                updated_rows=2,
                remaining_rows=3,
                batch_scanned_rows=2,
                batch_updated_rows=2,
                batch_unchanged_rows=0,
            ),
            _Result(
                status="completed",
                scanned_rows=5,
                updated_rows=4,
                unchanged_rows=1,
                remaining_rows=0,
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
        "run_raw_ownership_backfill_batch",
        apply,
    )

    assert (
        cli.main(
            ["--apply", "--batch-size", "2", "--max-batches", "9"]
        )
        == 0
    )
    payload, _rendered = _output(capsys)

    assert batch_sizes == [2, 2]
    assert len(factory.sessions) == 2
    assert [session.commits for session in factory.sessions] == [1, 1]
    assert [session.rollbacks for session in factory.sessions] == [0, 0]
    assert payload["mode"] == "apply"
    assert payload["status"] == "completed"
    assert payload["completed"] is True
    assert payload["batch_size"] == 2
    assert payload["max_batches"] == 9
    assert payload["batches_processed"] == 2
    assert payload["scanned_rows"] == 5
    assert payload["updated_rows"] == 4
    assert payload["unchanged_rows"] == 1
    assert payload["batch_scanned_rows"] == 3


def test_apply_stops_at_max_batches_while_checkpoint_is_running(
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
            scanned_rows=calls * 7,
            updated_rows=calls * 7,
            remaining_rows=50,
            batch_scanned_rows=7,
            batch_updated_rows=7,
            batch_unchanged_rows=0,
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "run_raw_ownership_backfill_batch",
        apply,
    )

    assert cli.main(["--apply", "--batch-size", "7", "--max-batches", "3"]) == 0
    payload, _rendered = _output(capsys)
    assert calls == 3
    assert len(factory.sessions) == 3
    assert all(session.commits == 1 for session in factory.sessions)
    assert payload["status"] == "running"
    assert payload["completed"] is False
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
        ["--phase", "raw_payloads"],
        ["--table", "raw_payloads"],
        ["--database-url", URL_SENTINEL],
        ["--app"],
        ["--batch", "2"],
        ["--reset"],
        ["--delete"],
        ["raw_payloads"],
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
        "operation": "subject_ownership_backfill",
        "phase": "stage3.raw_payloads.v1",
        "result": "error",
    }
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_restore_blocked_status_is_reported_without_claiming_completion(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)

    async def preflight(_session):
        return _Result(status="restore_blocked", remaining_rows=2)

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_raw_ownership_backfill",
        preflight,
    )

    assert cli.main([]) == 0
    payload, rendered = _output(capsys)

    assert len(factory.sessions) == 1
    assert payload["status"] == "restore_blocked"
    assert payload["completed"] is False
    assert payload["result"] == "ok"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


@pytest.mark.parametrize(
    ("error_name", "expected_code", "expected_exit"),
    [
        ("RawOwnershipBackfillValidationError", "validation_error", 2),
        ("RawOwnershipBackfillIdentityError", "identity_error", 1),
        ("RawOwnershipBackfillStateError", "state_error", 1),
        ("RawOwnershipBackfillMappingError", "mapping_error", 1),
        ("RawOwnershipBackfillDuplicateError", "duplicate_error", 1),
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
        "preflight_raw_ownership_backfill",
        fail,
    )

    assert cli.main([]) == expected_exit
    payload, rendered = _output(capsys)
    assert payload == {
        "error_code": expected_code,
        "format_version": 1,
        "mode": "status",
        "operation": "subject_ownership_backfill",
        "phase": "stage3.raw_payloads.v1",
        "result": "error",
    }
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_unexpected_and_unsafe_projection_failures_are_sanitized(
    monkeypatch,
    capsys,
):
    _install_factory(monkeypatch)

    async def fail(_session):
        raise RuntimeError(f"{PHI_SENTINEL} {URL_SENTINEL}")

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_raw_ownership_backfill",
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
        "preflight_raw_ownership_backfill",
        cancelled,
    )
    assert cli.main([]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "cancelled"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered

    async def unsafe(_session):
        result = _Result()
        result.to_safe_dict = lambda: {
            **_Result().to_safe_dict(),
            "status": PHI_SENTINEL,
        }
        return result

    monkeypatch.setattr(
        cli.backfill_service,
        "preflight_raw_ownership_backfill",
        unsafe,
    )
    assert cli.main([]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "internal_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered


def test_commit_failure_is_ambiguous_but_never_exposes_database_detail(
    monkeypatch,
    capsys,
):
    factory = _install_factory(monkeypatch)

    class _FailingCommitSession(_Session):
        async def commit(self) -> None:
            raise RuntimeError(f"{PHI_SENTINEL} {URL_SENTINEL}")

    factory.session_type = _FailingCommitSession

    async def apply(_session, *, batch_size):
        assert batch_size == 2
        return _Result(
            batch_scanned_rows=2,
            batch_updated_rows=2,
            batch_unchanged_rows=0,
        )

    monkeypatch.setattr(
        cli.backfill_service,
        "run_raw_ownership_backfill_batch",
        apply,
    )

    assert cli.main(["--apply", "--batch-size", "2"]) == 1
    payload, rendered = _output(capsys)
    assert payload["error_code"] == "internal_error"
    assert PHI_SENTINEL not in rendered
    assert URL_SENTINEL not in rendered
