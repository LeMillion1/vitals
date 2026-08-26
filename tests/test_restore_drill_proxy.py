"""The restore browser proxy is fixed, credential-free, and opt-in."""

from __future__ import annotations

import asyncio

import pytest

from scripts import serve_restore_drill_proxy as proxy


def test_proxy_has_one_fixed_internal_target():
    assert proxy.LISTEN_HOST == "0.0.0.0"
    assert proxy.LISTEN_PORT == 8000
    assert proxy.TARGET_HOST == "vitals_app"
    assert proxy.TARGET_PORT == 8000


def test_proxy_refuses_start_without_exact_drill_marker(monkeypatch):
    monkeypatch.delenv("VITALS_RESTORE_DRILL_PROXY", raising=False)

    with pytest.raises(RuntimeError, match="restore drill proxy marker missing"):
        asyncio.run(proxy._serve())
