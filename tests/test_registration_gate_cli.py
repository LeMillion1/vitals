"""The deployment registration gate is an explicit owner-only file change."""

from __future__ import annotations

import json
import stat

from scripts import registration_gate as cli


def _runtime_env(tmp_path, *, gate: str = "0"):
    runtime_dir = tmp_path / "runtime-config"
    runtime_dir.mkdir(mode=0o700)
    runtime_dir.chmod(0o700)
    runtime = runtime_dir / "vitals.env"
    runtime.write_text(
        "# synthetic runtime\n"
        "VITALS_DATABASE_URL=sqlite+aiosqlite:///synthetic.db\n"
        f"VITALS_REGISTRATION_UNLOCKED={gate}\n"
        "VITALS_SESSION_SECRET=keep-this-secret\n",
        encoding="utf-8",
    )
    runtime.chmod(0o600)
    return runtime


def test_status_is_an_explicit_owner_only_readback(tmp_path, capsys):
    runtime = _runtime_env(tmp_path)

    assert cli._run(cli._parse_args(["--runtime-env", str(runtime)])) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "operation": "registration_gate",
        "readback": "locked",
        "result": "ok",
        "runtime_env": str(runtime),
    }


def test_unlock_requires_exact_stopped_web_confirmation(tmp_path, capsys):
    runtime = _runtime_env(tmp_path)
    original = runtime.read_text(encoding="utf-8")

    assert (
        cli._run(
            cli._parse_args(
                [
                    "--runtime-env",
                    str(runtime),
                    "--set",
                    "unlocked",
                    "--confirm",
                    "UNLOCK REGISTRATION",
                ]
            )
        )
        == 2
    )

    assert runtime.read_text(encoding="utf-8") == original
    assert "exact --confirm" in json.loads(capsys.readouterr().err)["reason"]


def test_unlock_is_atomic_private_and_reports_required_recreate(tmp_path, capsys):
    runtime = _runtime_env(tmp_path)

    assert (
        cli._run(
            cli._parse_args(
                [
                    "--runtime-env",
                    str(runtime),
                    "--set",
                    "unlocked",
                    "--confirm",
                    cli.UNLOCK_CONFIRMATION,
                ]
            )
        )
        == 0
    )

    content = runtime.read_text(encoding="utf-8")
    assert "VITALS_REGISTRATION_UNLOCKED=1\n" in content
    assert "VITALS_SESSION_SECRET=keep-this-secret\n" in content
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
    result = json.loads(capsys.readouterr().out)
    assert result["previous"] == "locked"
    assert result["readback"] == "unlocked"
    assert "recreate and health-check vitals_app" in result["next_action"]


def test_lock_requires_its_own_exact_confirmation_and_readback(tmp_path, capsys):
    runtime = _runtime_env(tmp_path, gate="1")

    assert (
        cli._run(
            cli._parse_args(
                [
                    "--runtime-env",
                    str(runtime),
                    "--set",
                    "locked",
                    "--confirm",
                    cli.LOCK_CONFIRMATION,
                ]
            )
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["previous"] == "unlocked"
    assert result["readback"] == "locked"
    assert "VITALS_REGISTRATION_UNLOCKED=0" in runtime.read_text(encoding="utf-8")


def test_gate_change_refuses_a_permissive_runtime_directory(tmp_path, capsys):
    runtime = _runtime_env(tmp_path)
    runtime.parent.chmod(0o755)

    assert (
        cli._run(
            cli._parse_args(
                [
                    "--runtime-env",
                    str(runtime),
                    "--set",
                    "unlocked",
                    "--confirm",
                    cli.UNLOCK_CONFIRMATION,
                ]
            )
        )
        == 2
    )

    result = json.loads(capsys.readouterr().err)
    assert "mode 0700" in result["reason"]
    assert "VITALS_REGISTRATION_UNLOCKED=0" in runtime.read_text(encoding="utf-8")


def test_gate_readback_refuses_an_ambiguous_boolean(tmp_path, capsys):
    runtime = _runtime_env(tmp_path, gate="perhaps")

    assert cli._run(cli._parse_args(["--runtime-env", str(runtime)])) == 2

    result = json.loads(capsys.readouterr().err)
    assert "unsupported boolean" in result["reason"]


def test_gate_canonicalizes_an_exported_assignment_without_duplicating_it(
    tmp_path, capsys
):
    runtime = _runtime_env(tmp_path)
    runtime.write_text(
        runtime.read_text(encoding="utf-8").replace(
            "VITALS_REGISTRATION_UNLOCKED=0",
            "export VITALS_REGISTRATION_UNLOCKED=0",
        ),
        encoding="utf-8",
    )
    runtime.chmod(0o600)

    assert (
        cli._run(
            cli._parse_args(
                [
                    "--runtime-env",
                    str(runtime),
                    "--set",
                    "unlocked",
                    "--confirm",
                    cli.UNLOCK_CONFIRMATION,
                ]
            )
        )
        == 0
    )

    content = runtime.read_text(encoding="utf-8")
    assert content.count("VITALS_REGISTRATION_UNLOCKED=") == 1
    assert "VITALS_REGISTRATION_UNLOCKED=1\n" in content
    assert json.loads(capsys.readouterr().out)["readback"] == "unlocked"


def test_gate_refuses_a_runtime_file_containing_operator_authority(
    tmp_path, capsys
):
    runtime = _runtime_env(tmp_path)
    runtime.write_text(
        runtime.read_text(encoding="utf-8")
        + "VITALS_MIGRATION_DATABASE_URL=postgresql+asyncpg://owner@db/vitals\n",
        encoding="utf-8",
    )
    runtime.chmod(0o600)

    assert cli._run(cli._parse_args(["--runtime-env", str(runtime)])) == 2

    result = json.loads(capsys.readouterr().err)
    assert "non-runtime keys" in result["reason"]
