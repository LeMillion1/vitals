"""Browser for the curated conflict-rule catalog (vitals/data/conflict_rules.yaml).

Read-only browsing + an active/inactive toggle per rule; the rules themselves
are authored in the YAML and upserted by conflict_catalog.sync_catalog — this
page never creates/edits rule content, only flips the one field sync_catalog
leaves alone.
"""
from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

from typing import Optional

from fastapi import APIRouter, Depends, Form, Path, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.conflict_rule import ConflictRule
from vitals.services.conflicts import activation, engine
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
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
    context: alerts_service_contracts.HealthAlertContext,
) -> set[int]:
    """Rule ids with an active (unresolved) alert right now — the conflict engine
    stamps ``alert_key = f"conflict:{rule_id}"`` (see conflict_engine.enforce)."""
    active = await alerts_service_lifecycle.list_active_scoped(
        db,
        context=context,
        legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
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
    alert_context = alerts_service_contracts.HealthAlertContext(ownership.owner_action())
    conflict_scope = await engine.resolve_legacy_conflict_scope(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    catalog_rules = list(
        await engine.load_scoped_rules(
            db,
            scope=conflict_scope,
            active_only=False,
        )
    )
    activation_state = await activation.read_activation_state(
        db,
        subject_id=conflict_scope.subject_id,
        legacy_bridge=conflict_scope.legacy_bridge,
    )
    rule_activation = activation.effective_rule_activation(
        catalog_rules,
        activation_state,
    )
    catalog_rules.sort(key=lambda row: (row.category or "", row.code or ""))
    rules = list(catalog_rules)

    if domain:
        rules = [r for r in rules if r.domain_a == domain or r.domain_b == domain]
    if severity:
        rules = [r for r in rules if r.severity == severity]

    firing_ids = await _firing_rule_ids(db, context=alert_context)
    firing_ids &= {
        rule_id for rule_id, is_active in rule_activation.items() if is_active
    }

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
            "rule_activation": rule_activation,
            "domain_filter": domain or "",
            "severity_filter": severity or "",
            "all_domains": all_domains,
            "total_count": len(rules),
        },
    )


@router.post("/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int = Path(..., gt=0),
    active: bool = Form(...),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_scope = await engine.resolve_legacy_conflict_scope(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    try:
        await activation.set_rule_activation(
            db,
            subject_id=conflict_scope.subject_id,
            rule_id=rule_id,
            active=active,
            legacy_bridge=conflict_scope.legacy_bridge,
        )
    except (
        activation.ConflictActivationRuleNotFoundError,
        activation.ConflictActivationOwnershipError,
    ):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    if not active:
        ownership = await resolve_legacy_ownership_context(
            db,
            actor_username=username,
        )
        await alerts_service_lifecycle.resolve_scoped_superseded(
            db,
            context=alerts_service_contracts.HealthAlertContext(ownership.owner_action()),
            alert_key=f"conflict:{rule_id}",
            keep_entity=None,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
