"""Unit contracts for the isolated installation restore drill."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from urllib import error as urlerror

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_script(
    "vitals_restore_drill_runner", "scripts/rehearse_installation_restore.py"
)
login = _load_script(
    "vitals_prepare_restore_drill_login", "scripts/prepare_restore_drill_login.py"
)

STAMP = "20260826T120000Z"


def _assert_drill_error(expected_code: str, function, *args, **kwargs) -> None:
    with pytest.raises(runner.DrillError, match=f"^{expected_code}$"):
        function(*args, **kwargs)


def test_subprocess_failure_surfaces_only_a_bounded_error_code(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["synthetic"],
        1,
        stdout=b"",
        stderr=(
            b"compose prefix \x1b[31m"
            b'{"result":"error","error_code":"subject_data_missing"}\x1b[0m\n'
        ),
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    _assert_drill_error(
        "runtime_rls_validation_failed_subject_data_missing",
        runner._run,
        ["synthetic"],
        code="runtime_rls_validation_failed",
    )


def test_subprocess_failure_rejects_unbounded_error_detail(monkeypatch):
    completed = subprocess.CompletedProcess(
        ["synthetic"],
        1,
        stdout=b'{"result":"error","error_code":"dsn=secret"}\n',
        stderr=b"sensitive database diagnostics",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    _assert_drill_error(
        "runtime_rls_validation_failed",
        runner._run,
        ["synthetic"],
        code="runtime_rls_validation_failed",
    )


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        (b"OSError: [Errno 30] Read-only file system: secret", "drill_app_read_only_failure"),
        (b"password authentication failed for user hidden", "drill_app_database_auth_failure"),
        (b"Application startup failed. Exiting.", "drill_app_startup_failure"),
    ],
)
def test_app_failure_classification_returns_only_bounded_codes(
    tmp_path, monkeypatch, diagnostic, expected
):
    context = _context(tmp_path)

    def fake_run(command, **_kwargs):
        assert "logs" in command
        return subprocess.CompletedProcess(command, 0, stdout=diagnostic, stderr=b"")

    monkeypatch.setattr(runner, "_run", fake_run)

    assert runner._classify_app_failure(context) == expected


def test_app_failure_classification_uses_only_container_state_for_unknown_logs(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    calls = 0

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                command, 0, stdout=b"sensitive unknown detail", stderr=b""
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b'{"State":"exited","ExitCode":7}\n',
            stderr=b"",
        )

    monkeypatch.setattr(runner, "_run", fake_run)

    assert runner._classify_app_failure(context) == "drill_app_exited_7"


def test_http_wait_retains_bounded_error_status(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise urlerror.HTTPError(
            "http://127.0.0.1:18080/health",
            503,
            "sensitive reason",
            {},
            None,
        )

    monkeypatch.setattr(runner.urlrequest, "urlopen", unavailable)

    assert runner._wait_http(18080, "/health", timeout=0.01) == 503


def _bundle(tmp_path: Path) -> runner.Bundle:
    names = [
        f"vitals_{STAMP}.sql.gz",
        f"garmin_session_{STAMP}.tar.gz",
        f"private_files_{STAMP}.tar.gz",
        f"legacy_uploads_{STAMP}.tar.gz",
    ]
    entries = []
    for index, name in enumerate(names):
        content = f"synthetic-{index}".encode()
        (tmp_path / name).write_bytes(content)
        entries.append((hashlib.sha256(content).hexdigest(), name))
    manifest = tmp_path / f"vitals_bundle_{STAMP}.sha256"
    manifest.write_text(
        "".join(f"{digest}  {name}\n" for digest, name in entries),
        encoding="ascii",
    )
    return runner.Bundle(manifest=manifest, timestamp=STAMP, entries=tuple(entries))


def _make_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for info, content in members:
            archive.addfile(info, io.BytesIO(content) if info.isreg() else None)


def _file_member(name: str, content: bytes = b"synthetic") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o777
    return info, content


def _directory_member(name: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o777
    return info, b""


def _context(tmp_path: Path) -> runner.Context:
    run_id = "a1b2c3d4e5f6"
    run_dir = (tmp_path / f"run-{run_id}-fixture").resolve()
    run_dir.mkdir()
    source_dir = run_dir / "source"
    source_dir.mkdir()
    database = f"vitals_drill_{run_id}"
    owner = f"vitals_drill_owner_{run_id}"
    runtime_role = f"vitals_drill_runtime_{run_id}"
    compose_env = {
        "VITALS_APP_PORT": "18080",
        "VITALS_DB_NAME": f"vitals_drill_{run_id}",
        "VITALS_DB_PASSWORD": "owner-secret",
        "VITALS_DB_USER": owner,
        "VITALS_DATABASE_URL": (
            f"postgresql+asyncpg://{runtime_role}:runtime-secret@"
            f"vitals_db:5432/{database}"
        ),
        "VITALS_DRILL_GARMIN_DIR": str(run_dir / "garmin"),
        "VITALS_DRILL_LEGACY_UPLOAD_DIR": str(run_dir / "legacy_uploads"),
        "VITALS_DRILL_MCP_SECRET": "mcp-secret",
        "VITALS_DRILL_PRIVATE_FILES_DIR": str(run_dir / "private_files"),
        "VITALS_DRILL_RUNTIME_ENV_FILE": str(run_dir / "runtime.env"),
        "VITALS_DRILL_RUNTIME_PASSWORD": "runtime-secret",
        "VITALS_DRILL_SESSION_SECRET": "session-secret",
        "VITALS_MIGRATION_DATABASE_URL": (
            f"postgresql+asyncpg://{owner}:owner-secret@vitals_db:5432/{database}"
        ),
        "VITALS_RUNTIME_ENV_FILE": str(run_dir / "runtime.env"),
    }
    return runner.Context(
        run_id=run_id,
        project=f"vitals_drill_{run_id}",
        scratch_parent=tmp_path.resolve(),
        run_dir=run_dir,
        source_dir=source_dir,
        bundle_dir=run_dir / "bundle",
        operator_env=run_dir / "operator.env",
        runtime_env=run_dir / "runtime.env",
        state_file=run_dir / runner.STATE_NAME,
        marker_file=run_dir / runner.MARKER_NAME,
        port=18080,
        source_revision="a" * 40,
        bundle_timestamp=STAMP,
        manifest_sha256="b" * 64,
        database=database,
        owner_role=owner,
        runtime_role=runtime_role,
        compose_env=compose_env,
    )


def _write_bound_state(context: runner.Context) -> None:
    context.marker_file.write_text(
        json.dumps({"project": context.project, "run_id": context.run_id}),
        encoding="utf-8",
    )
    context.operator_env.write_text(runner._operator_content(context), encoding="utf-8")
    context.state_file.write_text(
        json.dumps(runner._state_payload(context, phase="served")),
        encoding="utf-8",
    )


def _set_login_env(monkeypatch, tmp_path: Path) -> Path:
    run_id = "a1b2c3d4e5f6"
    marker = tmp_path / "marker.json"
    marker.write_text(
        json.dumps({"project": f"vitals_drill_{run_id}", "run_id": run_id}),
        encoding="utf-8",
    )
    monkeypatch.setattr(login, "_MARKER_FILE", marker)
    values = {
        "VITALS_RESTORE_DRILL": "true",
        "VITALS_RESTORE_DRILL_MARKER_FILE": str(marker),
        "VITALS_DRILL_USERNAME": f"drill-{run_id}",
        "VITALS_DRILL_PASSWORD_HASH": runner.SYNTHETIC_HASH,
        "VITALS_DATABASE_URL": (
            f"postgresql+asyncpg://vitals_drill_owner_{run_id}:pass@"
            f"vitals_db:5432/vitals_drill_{run_id}"
        ),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return marker


def test_validate_manifest_accepts_only_the_exact_complete_bundle(tmp_path):
    expected = _bundle(tmp_path)

    actual = runner.validate_manifest(expected.manifest.resolve())

    assert actual.timestamp == STAMP
    assert actual.entries == expected.entries


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("wrong_name", "manifest_name_invalid"),
        ("missing_line", "manifest_line_count_invalid"),
        ("extra_line", "manifest_line_count_invalid"),
        ("wrong_order", "manifest_entry_invalid"),
        ("wrong_checksum", "bundle_checksum_mismatch"),
    ],
)
def test_validate_manifest_rejects_non_exact_or_changed_bundles(
    tmp_path, mutation, error
):
    bundle = _bundle(tmp_path)
    manifest = bundle.manifest
    lines = manifest.read_text(encoding="ascii").splitlines()
    if mutation == "wrong_name":
        manifest = manifest.rename(tmp_path / "bundle.sha256")
    elif mutation == "missing_line":
        manifest.write_text("\n".join(lines[:-1]) + "\n", encoding="ascii")
    elif mutation == "extra_line":
        manifest.write_text("\n".join([*lines, lines[-1]]) + "\n", encoding="ascii")
    elif mutation == "wrong_order":
        lines[0], lines[1] = lines[1], lines[0]
        manifest.write_text("\n".join(lines) + "\n", encoding="ascii")
    else:
        (tmp_path / f"vitals_{STAMP}.sql.gz").write_bytes(b"changed")

    _assert_drill_error(error, runner.validate_manifest, manifest.resolve())


def test_validate_manifest_rejects_symlinked_artifact(tmp_path):
    bundle = _bundle(tmp_path)
    artifact = tmp_path / bundle.entries[0][1]
    target = tmp_path / "outside.sql.gz"
    target.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(target)

    _assert_drill_error(
        "bundle_artifact_not_regular", runner.validate_manifest, bundle.manifest.resolve()
    )


def test_validate_manifest_rejects_symlinked_manifest(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    bundle = _bundle(source_dir)
    linked_dir = tmp_path / "linked"
    linked_dir.mkdir()
    link = linked_dir / bundle.manifest.name
    link.symlink_to(bundle.manifest)

    _assert_drill_error("manifest_not_regular", runner.validate_manifest, link.absolute())


@pytest.mark.parametrize("name", ["../escape", "dir/../../escape", "/absolute", "dir\\file"])
def test_tar_paths_cannot_escape_or_use_non_posix_separators(tmp_path, name):
    archive = tmp_path / "unsafe.tar.gz"
    _make_tar(archive, [_file_member(name)])

    _assert_drill_error("archive_path_invalid", runner.inspect_tar, archive)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_tar_links_are_rejected(tmp_path, kind):
    archive = tmp_path / "link.tar.gz"
    info = tarfile.TarInfo("linked")
    info.type = kind
    info.linkname = "target"
    _make_tar(archive, [(info, b"")])

    _assert_drill_error("archive_entry_type_invalid", runner.inspect_tar, archive)


def test_tar_duplicate_paths_are_rejected(tmp_path):
    archive = tmp_path / "duplicate.tar.gz"
    _make_tar(archive, [_file_member("same"), _file_member("same", b"other")])

    _assert_drill_error("archive_duplicate_path", runner.inspect_tar, archive)


def test_tar_extraction_ignores_archive_modes_and_hardens_output(tmp_path):
    archive = tmp_path / "valid.tar.gz"
    _make_tar(
        archive,
        [_directory_member("nested"), _file_member("nested/data.bin", b"private")],
    )
    destination = tmp_path / "output"

    assert runner.extract_tar(archive, destination) == len(b"private")
    assert (destination / "nested" / "data.bin").read_bytes() == b"private"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "nested").stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "nested" / "data.bin").stat().st_mode) == 0o600


def test_scratch_parent_must_be_absolute(tmp_path, monkeypatch):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    bundle = _bundle(bundle_dir)
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.chdir(tmp_path)

    _assert_drill_error(
        "scratch_parent_not_absolute",
        runner._validate_scratch_parent,
        Path("scratch"),
        repository=repository.resolve(),
        bundle=bundle,
    )


def test_scratch_parent_rejects_symlinks_and_sensitive_ancestors(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    bundle_dir = tmp_path / "backup"
    bundle_dir.mkdir()
    bundle = _bundle(bundle_dir)
    real = tmp_path / "real-scratch"
    real.mkdir()
    symlink = tmp_path / "scratch-link"
    symlink.symlink_to(real, target_is_directory=True)

    _assert_drill_error(
        "scratch_parent_symlink",
        runner._validate_scratch_parent,
        symlink,
        repository=repository,
        bundle=bundle,
    )
    for forbidden in (repository, bundle_dir):
        _assert_drill_error(
            "scratch_parent_forbidden",
            runner._validate_scratch_parent,
            forbidden,
            repository=repository,
            bundle=bundle,
        )
        child = forbidden / "scratch"
        _assert_drill_error(
            "scratch_parent_forbidden",
            runner._validate_scratch_parent,
            child,
            repository=repository,
            bundle=bundle,
        )


@pytest.mark.parametrize("port", [-1, 0, 1024, 8000, 65536])
def test_port_guard_rejects_reserved_or_invalid_ports(port):
    assert runner._port_available(port) is False


def test_port_guard_rejects_an_in_use_loopback_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    try:
        assert runner._port_available(listener.getsockname()[1]) is False
    finally:
        listener.close()


def test_state_loader_binds_run_directory_marker_and_project(tmp_path):
    context = _context(tmp_path)
    _write_bound_state(context)

    loaded, state = runner._context_from_state(context.run_dir)

    assert loaded.run_dir == context.run_dir
    assert loaded.scratch_parent == context.scratch_parent
    assert loaded.project == context.project
    assert loaded.run_id == context.run_id
    assert state["phase"] == "served"


@pytest.mark.parametrize("tamper", ["marker", "project", "run_dir", "scratch_parent"])
def test_state_loader_rejects_broken_run_bindings(tmp_path, tamper):
    context = _context(tmp_path)
    _write_bound_state(context)
    if tamper == "marker":
        context.marker_file.write_text("{}", encoding="utf-8")
    else:
        state = json.loads(context.state_file.read_text(encoding="utf-8"))
        if tamper == "project":
            state["project"] = "vitals_drill_ffffffffffff"
        elif tamper == "run_dir":
            state["run_dir"] = str(tmp_path)
        else:
            state["scratch_parent"] = str(context.run_dir)
        context.state_file.write_text(json.dumps(state), encoding="utf-8")

    _assert_drill_error("run_state_invalid", runner._context_from_state, context.run_dir)


def test_cleanup_removes_only_a_marker_bound_run_directory(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _write_bound_state(context)
    monkeypatch.setattr(runner, "_resource_ids", lambda _project: {})

    runner._cleanup(context)

    assert not context.run_dir.exists()


def test_cleanup_refuses_a_marker_for_another_project(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _write_bound_state(context)
    context.marker_file.write_text(
        json.dumps({"project": "vitals_drill_ffffffffffff", "run_id": context.run_id}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_resource_ids", lambda _project: {})

    _assert_drill_error("scratch_cleanup_guard_failed", runner._cleanup, context)
    assert context.run_dir.exists()


def test_cleanup_uses_the_bound_compose_project_and_verifies_removal(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    _write_bound_state(context)
    context.docker_mutated = True
    inspections = iter([{"containers": [], "networks": [], "volumes": []}])
    commands = []
    monkeypatch.setattr(runner, "_resource_ids", lambda project: next(inspections))

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner, "_run", fake_run)

    runner._cleanup(context, remove_directory=False)

    command, kwargs = commands[0]
    project_index = command.index("--project-name") + 1
    assert command[project_index] == context.project
    assert command[-5:] == [
        "down",
        "--volumes",
        "--remove-orphans",
        "--timeout",
        "10",
    ]
    assert kwargs["cwd"] == context.source_dir
    assert context.run_dir.exists()


def test_service_run_mounts_marker_file_read_only(tmp_path, monkeypatch):
    context = _context(tmp_path)
    marker = context.run_dir / "marker.json"
    marker.write_text("{}", encoding="utf-8")
    captured = []

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner, "_run", fake_run)

    runner._service_run(
        context,
        "vitals_migrate",
        ["python", "helper.py"],
        code="helper_failed",
        bind_files=((marker, "/run/vitals-restore-drill/marker.json"),),
    )

    command, _kwargs = captured[0]
    volume_index = command.index("-v") + 1
    assert command[volume_index] == (
        f"{marker}:/run/vitals-restore-drill/marker.json:ro"
    )


def test_restore_compose_overlay_keeps_drill_inputs_read_only_and_network_internal():
    overlay = (ROOT / "docker-compose.restore-drill.yml").read_text(encoding="utf-8")

    assert "internal: true" in overlay
    assert 'profiles: ["restore-drill-disabled"]' in overlay
    assert overlay.count("read_only: true") == 5
    assert overlay.count("create_host_path: false") == 4
    for target in (
        "/app/.env",
        "/data/garmin_session",
        "/app/web/static/uploads",
        "/data/private_files",
    ):
        assert f"target: {target}" in overlay


def test_login_preparation_rejects_missing_marker_and_non_drill_database(
    tmp_path, monkeypatch
):
    _set_login_env(monkeypatch, tmp_path)
    monkeypatch.setenv("VITALS_RESTORE_DRILL", "false")
    with pytest.raises(RuntimeError, match="^restore_drill_marker_invalid$"):
        asyncio.run(login._prepare())

    monkeypatch.setenv("VITALS_RESTORE_DRILL", "true")
    monkeypatch.setenv(
        "VITALS_DATABASE_URL", "postgresql+asyncpg://user:pass@db/vitals_production"
    )
    with pytest.raises(RuntimeError, match="^database_url_invalid$"):
        asyncio.run(login._prepare())


def test_login_preparation_rejects_symlinked_or_mismatched_marker(tmp_path, monkeypatch):
    marker = _set_login_env(monkeypatch, tmp_path)
    target = tmp_path / "real-marker.json"
    marker.rename(target)
    marker.symlink_to(target)

    with pytest.raises(RuntimeError, match="^restore_drill_marker_invalid$"):
        asyncio.run(login._prepare())

    marker.unlink()
    marker.write_text(
        json.dumps(
            {
                "project": "vitals_drill_ffffffffffff",
                "run_id": "ffffffffffff",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="^restore_drill_marker_invalid$"):
        asyncio.run(login._prepare())


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("VITALS_DRILL_USERNAME", "admin", "drill_username_invalid"),
        ("VITALS_DRILL_PASSWORD_HASH", "plaintext", "drill_password_hash_invalid"),
        ("VITALS_DATABASE_URL", "sqlite:///local.db", "database_url_invalid"),
        (
            "VITALS_DATABASE_URL",
            "postgresql+asyncpg://vitals_drill_owner_a1b2c3d4e5f6:pass@"
            "other:5432/vitals_drill_a1b2c3d4e5f6",
            "database_url_invalid",
        ),
        (
            "VITALS_DATABASE_URL",
            "postgresql+asyncpg://wrong:pass@"
            "vitals_db:5432/vitals_drill_a1b2c3d4e5f6",
            "database_url_invalid",
        ),
    ],
)
def test_login_preparation_rejects_unscoped_credentials(
    tmp_path, monkeypatch, key, value, error
):
    _set_login_env(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match=f"^{error}$"):
        asyncio.run(login._prepare())


def test_apply_validation_requires_an_explicit_completed_flag(tmp_path, monkeypatch):
    context = _context(tmp_path)
    bundle_dir = tmp_path / "input"
    bundle_dir.mkdir()
    bundle = _bundle(bundle_dir)
    cleanup_calls = []

    monkeypatch.setattr(runner, "validate_manifest", lambda _path: bundle)
    monkeypatch.setattr(runner, "_build_context", lambda *_args: context)
    monkeypatch.setattr(runner, "_write_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runner, "stage_bundle", lambda *_args: bundle)
    monkeypatch.setattr(runner, "verify_gzip", lambda _path: 1)
    monkeypatch.setattr(runner, "extract_tar", lambda *_args: 0)
    monkeypatch.setattr(runner, "_restore_database", lambda *_args: None)
    monkeypatch.setattr(runner, "_write_owner_only", lambda *_args: None)
    monkeypatch.setattr(runner, "_project_absent", lambda _project: None)
    monkeypatch.setattr(runner, "_render_and_assert", lambda _context: {})
    monkeypatch.setattr(
        runner,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=b"ef01 (head)\n" if command[-2:] == ["alembic", "heads"] else b"",
            stderr=b"",
        ),
    )
    psql_results = iter(["0", "abcd", "ef01"])
    monkeypatch.setattr(runner, "_psql", lambda *_args, **_kwargs: next(psql_results))

    def fake_service_run(_context, _service, command, **_kwargs):
        if command[0] == "alembic":
            stdout = b"ef01 (head)\n" if command[1] == "heads" else b""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")
        assert command[1] == "scripts/validate_subject_ownership.py"
        payload = {"result": "ok", "status": "completed"}
        # The mutating apply phase must attest completion independently of status.
        if "--apply" not in command:
            payload["status"] = "clean"
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    monkeypatch.setattr(runner, "_service_run", fake_service_run)
    monkeypatch.setattr(
        runner, "_cleanup", lambda value: cleanup_calls.append(value.project)
    )
    args = SimpleNamespace(
        manifest=bundle.manifest,
        scratch_parent=tmp_path,
        port=18080,
        serve=False,
    )

    _assert_drill_error("ownership_validation_failed", runner.run_drill, args)
    assert cleanup_calls == [context.project]
