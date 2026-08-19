"""/interactions — the curated conflict-rule catalog browser + toggle."""
from __future__ import annotations

import re

from sqlalchemy import select

from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject
from vitals.models.scoped_settings import SubjectSetting
from vitals.models.system_alert import SystemAlert
from vitals.services import conflict_catalog, conflict_activation_service


async def test_dashboard_renders_synced_catalog(auth_client, db_session):
    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()

    r = await auth_client.get("/interactions", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "Взаимодействия" in r.text
    # A rule from each of the two dermatology seed codes should render somewhere.
    assert "Ретиноид и пилинг" in r.text


async def test_dashboard_filters_by_domain(auth_client, db_session):
    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()

    r = await auth_client.get("/interactions", params={"domain": "genetics"}, headers={"Accept": "text/html"})
    assert r.status_code == 200
    result = await db_session.execute(select(ConflictRule).where(ConflictRule.domain_a == "genetics"))
    some_genetics_rule = result.scalars().first()
    assert some_genetics_rule.message in r.text


async def test_toggle_flips_active_and_persists(auth_client, db_session):
    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()

    result = await db_session.execute(select(ConflictRule).limit(1))
    rule = result.scalar_one()
    assert rule.active is True
    alert = SystemAlert(
        domain=rule.domain_a,
        severity=rule.severity,
        message=rule.message,
        alert_key=f"conflict:{rule.id}",
        entity_ref="toggle-test",
    )
    db_session.add(alert)
    await db_session.commit()

    r = await auth_client.post(f"/interactions/{rule.id}/toggle", data={"active": "false"})
    assert r.status_code == 204

    await db_session.refresh(rule)
    await db_session.refresh(alert)
    assert rule.active is False
    subject_id = await db_session.scalar(select(HealthSubject.id))
    setting = await db_session.get(
        SubjectSetting,
        (subject_id, conflict_activation_service.SETTING_KEY),
    )
    assert setting.value == {
        "v": 1,
        "disabled_codes": [rule.code],
    }
    assert alert.resolved_at is not None
    assert alert.resolved_by_user_id is not None


async def test_dashboard_uses_subject_activation_instead_of_global_flag(
    auth_client,
    db_session,
):
    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()
    rule = await db_session.scalar(
        select(ConflictRule).where(ConflictRule.code.is_not(None)).limit(1)
    )

    response = await auth_client.post(
        f"/interactions/{rule.id}/toggle",
        data={"active": "false"},
    )
    assert response.status_code == 204

    # Simulate a stale legacy mirror. The subject setting is authoritative for
    # the rendered state and must not be replaced by this global flag.
    rule.active = True
    await db_session.commit()

    page = await auth_client.get("/interactions", headers={"Accept": "text/html"})
    switch = re.search(
        rf'<input[^>]*hx-post="/interactions/{rule.id}/toggle"[^>]*>',
        page.text,
        flags=re.DOTALL,
    )
    assert switch is not None
    assert re.search(r"\schecked(?:\s|>)", switch.group(0)) is None


async def test_toggle_unknown_rule_404s(auth_client):
    r = await auth_client.post("/interactions/999999/toggle", data={"active": "false"})
    assert r.status_code == 404


async def test_toggle_rejects_non_positive_rule_id(auth_client):
    response = await auth_client.post(
        "/interactions/0/toggle",
        data={"active": "false"},
    )
    assert response.status_code == 422


async def test_firing_now_badge_reflects_active_alert(auth_client, db_session):
    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()

    result = await db_session.execute(select(ConflictRule).limit(1))
    rule = result.scalar_one()
    db_session.add(
        SystemAlert(
            domain=rule.domain_a,
            severity=rule.severity,
            message=rule.message,
            alert_key=f"conflict:{rule.id}",
            entity_ref="test",
        )
    )
    await db_session.commit()

    r = await auth_client.get("/interactions", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "Срабатывает сейчас" in r.text
