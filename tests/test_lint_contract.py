"""The linter runs, and the repository satisfies it.

A configuration file nobody executes drifts from the code within a week. This
runs ruff over the whole repository as an ordinary test, so a defect it can see
fails the suite rather than waiting for somebody to remember the command.

Scope is correctness, not style: undefined names, imports of deleted things,
``__all__`` entries with nothing behind them, dictionary keys written twice so
the first value is silently discarded. Two of those were introduced and caught
on the day this was added — an ``HTMLResponse`` that was never imported, on the
one branch of an error handler no test exercised, and an MCP tool that had never
imported the service it calls.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _ruff() -> str | None:
    for candidate in (REPOSITORY_ROOT / ".venv" / "bin" / "ruff", Path("ruff")):
        resolved = shutil.which(str(candidate))
        if resolved:
            return resolved
    return None


def test_the_repository_passes_its_own_lint_contract():
    ruff = _ruff()
    if ruff is None:
        pytest.skip("ruff is not installed in this environment")

    result = subprocess.run(
        [ruff, "check", "--no-cache", "."],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        "ruff found something worth fixing:\n\n" + result.stdout + result.stderr
    )


def test_the_configuration_still_selects_the_correctness_rules():
    """The value here is the rule set, so a quiet narrowing should be loud.

    Dropping ``F`` would leave the file in place, the test passing, and nothing
    being checked — the failure mode a lint config is most prone to.
    """

    config = (REPOSITORY_ROOT / "ruff.toml").read_text()
    for rule in ('"F"', '"E4"', '"E7"', '"E9"', '"W"'):
        assert rule in config, f"ruff.toml no longer selects {rule}"
