"""Browser for the curated conflict-rule catalog (vitals/data/conflict_rules.yaml).

Read-only browsing + an active/inactive toggle per rule; the rules themselves
are authored in the YAML and upserted by conflict_catalog.sync_catalog — this
page never creates/edits rule content, only flips the one field sync_catalog
leaves alone.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.conflict_rule import ConflictRule
from vitals.services import alerts_service, conflict_engine
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/interactions", tags=["interactions"])

# Category display order — anything not listed (or null) sorts last under "other".
_CATEGORY_ORDER = (
    "absorption", "pharmacogenomics", "dermatology", "lab_safety", "glp1", "contraindication",
)


async def _firing_rule_ids(
    db: AsyncSession,
    *,
    context: alerts_service.HealthAlertContext,
) -> set[int]:
    """Rule ids with an active (unresolved) alert right now — the conflict engine
    stamps ``alert_key = f"conflict:{rule_id}"`` (see conflict_engine.enforce)."""
    active = await alerts_service.list_active_scoped(
        db,
        context=context,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )
    ids: set[int] = set()
    for row in active:
        alert_key = row.alert_key
        if not alert_key.startswith("conflict:"):
            continue
        _, _, raw_id = alert_key.partition(":")
        if raw_id.isdigit():
            ids.add(int(raw_id))
    return ids


@router.get("", response_class=HTMLResponse)
async def interactions_dashboard(
    request: Request,
    domain: Optional[str] = None,
    severity: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    alert_context = alerts_service.HealthAlertContext(ownership.owner_action())
    conflict_scope = await conflict_engine.resolve_legacy_conflict_scope(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    catalog_rules = list(
        await conflict_engine.load_scoped_rules(
            db,
            scope=conflict_scope,
            active_only=False,
        )
    )
    catalog_rules.sort(key=lambda row: (row.category or "", row.code or ""))
    rules = list(catalog_rules)

    if domain:
        rules = [r for r in rules if r.domain_a == domain or r.domain_b == domain]
    if severity:
        rules = [r for r in rules if r.severity == severity]

    firing_ids = await _firing_rule_ids(db, context=alert_context)

    by_category: dict[str, list[ConflictRule]] = {}
    for r in rules:
        by_category.setdefault(r.category or "other", []).append(r)
    ordered_categories = [c for c in _CATEGORY_ORDER if c in by_category]
    ordered_categories += sorted(c for c in by_category if c not in _CATEGORY_ORDER)

    # Filter dropdown always lists every domain in the *unfiltered* catalog, so
    # switching away from the active filter is always possible.
    all_domains = sorted(
        {
            rule_domain
            for row in catalog_rules
            for rule_domain in (row.domain_a, row.domain_b)
        }
    )

    return templates.TemplateResponse(
        request,
        "interactions/index.html",
        {
            "username": username,
            "by_category": by_category,
            "ordered_categories": ordered_categories,
            "firing_ids": firing_ids,
            "domain_filter": domain or "",
            "severity_filter": severity or "",
            "all_domains": all_domains,
            "total_count": len(rules),
        },
    )


@router.post("/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int,
    active: bool = Form(...),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    row = await db.get(ConflictRule, rule_id)
    if row is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    row.active = active
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
