"""MCP body-composition tools — read/write/history/delete, module gating, and
notes. Skipped where FastMCP can't import (the read-only MCP tests share this
constraint); runs in the real environment / Postgres CI."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

mcp_router = pytest.importorskip("web.routers.mcp")

from vitals.models.identity import HealthSubject  # noqa: E402
from vitals.services.modules import preferences as modules_service  # noqa: E402


@pytest.fixture(autouse=True)
def _materialize_legacy_owner(legacy_owner_roots):
    return legacy_owner_roots


async def _enable_body_comp(db_session):
    subject_ids = list(
        await db_session.scalars(select(HealthSubject.id).order_by(HealthSubject.id))
    )
    assert len(subject_ids) == 1
    await modules_service.set_module_enabled(
        db_session,
        key="body_comp",
        enabled=True,
        subject_id=subject_ids[0],
    )
    await db_session.commit()


async def test_log_get_history_delete_body_scan(db_session, session_factory, monkeypatch, owner_write, legacy_owner_roots):
    await _enable_body_comp(db_session)
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    res = await mcp_router.log_body_scan(
        metrics=[
            {"label": "Процент жира", "value": 18.5, "unit": "%"},
            {"label": "Вес", "value": 80.0, "unit": "кг"},
        ],
        on_date="2026-06-10",
        device="InBody 770",
    )
    assert "error" not in res
    assert res["device"] == "InBody 770"
    assert any(m["metric_key"] == "body_fat_pct" for m in res["metrics"])
    scan_id = res["id"]

    # The scan's weight was bridged into the weight domain as a body_scan source.
    from vitals.services import weight as weight_domain
    active = await weight_domain.logs.get_active_weight(
        db_session,
        date(2026, 6, 10),
        subject_id=owner_write.subject_id,
    )
    assert active is not None and active.source == "body_scan"

    scans = await mcp_router.get_body_scans(start_date="2026-06-01", end_date="2026-06-30")
    assert len(scans) == 1 and scans[0]["metrics"]

    one = await mcp_router.get_body_scan(scan_id)
    assert one["id"] == scan_id

    hist = await mcp_router.get_body_metric_history("body_fat_pct")
    assert hist and hist[0]["value"] == 18.5

    deleted = await mcp_router.delete_record("body_comp", scan_id)
    assert deleted["deleted"] is True
    assert "error" in await mcp_router.get_body_scan(scan_id)


async def test_tools_blocked_when_module_disabled(db_session, session_factory, monkeypatch):
    # No app_settings row → body_comp is off by default; direct calls must refuse
    # even though tools/list normally hides the whole optional surface.
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    res = await mcp_router.log_body_scan(metrics=[{"label": "Вес", "value": 80.0}], on_date="2026-06-10")
    assert res.get("error")
    d = await mcp_router.delete_record("body_comp", 1)
    assert d.get("error")
    assert (await mcp_router.get_body_scans())[0].get("error")
    assert (await mcp_router.get_body_scan(1)).get("error")
    assert (await mcp_router.get_body_metric_history("body_fat_pct"))[0].get(
        "error"
    )


async def test_notes_for_body_comp_domain(db_session, session_factory, monkeypatch, legacy_owner_roots):
    await _enable_body_comp(db_session)
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    res = await mcp_router.log_body_scan(metrics=[{"label": "Вес", "value": 80.0}], on_date="2026-06-10")
    sid = res["id"]

    noted = await mcp_router.log_note(domain="body_comp", record_id=sid, note="клиника X")
    assert "error" not in noted and noted["note"] == "клиника X"

    notes = await mcp_router.get_notes(domain="body_comp")
    assert any(n.get("note") == "клиника X" for n in notes)
