"""Custom chart builder — a Core, cross-domain utility (not gated by any single
Optional module, since it exists specifically to overlay metrics *across*
domains)."""
from __future__ import annotations

from vitals.services.timeline import annotations as timeline_annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.charts import configuration as custom_charts_service
from vitals.services.charts import data as chart_data_service
from vitals.services.charts.configuration import ChartConfigError
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from web.deps import get_redis, get_session, require_auth
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/charts", tags=["charts"])


@router.get("", response_class=HTMLResponse)
async def charts_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    lang = getattr(request.state, "lang", "ru")
    enabled = getattr(request.state, "enabled_modules", None) or {}

    catalog = await chart_data_service.build_catalog(
        db, enabled, subject_id=ownership.subject_id, lang=lang
    )
    charts = await custom_charts_service.list_charts(
        db,
        redis,
        subject_id=ownership.subject_id,
    )
    resolved = {
        c["id"]: await chart_data_service.resolve_chart_series(
            db, c, subject_id=ownership.subject_id, lang=lang
        )
        for c in charts
    }
    overlays = (
        await _overlays_by_chart(
            db,
            charts,
            subject_id=ownership.subject_id,
        )
        if enabled.get("timeline")
        else {}
    )

    return templates.TemplateResponse(
        request,
        "charts/index.html",
        {
            "username": username,
            "catalog": catalog,
            "charts": charts,
            "resolved": resolved,
            "overlays": overlays,
            "error": request.query_params.get("error"),
        },
    )


async def _overlays_by_chart(
    db: AsyncSession,
    charts: list[dict],
    *,
    subject_id,
) -> dict[str, list[dict]]:
    """Manual Timeline flags for each saved chart — the union of its series'
    domains, deduped (a global flag would otherwise repeat once per domain)."""


    result: dict[str, list[dict]] = {}
    for c in charts:
        domains = {s.get("domain") for s in c["series"] if s.get("domain")}
        seen: set[tuple] = set()
        merged: list[dict] = []
        for d in domains:
            for o in await timeline_annotations.overlays_for(
                db,
                subject_id=subject_id,
                domain=d,
            ):
                key = (o["start"], o["end"], o["label"])
                if key in seen:
                    continue
                seen.add(key)
                merged.append(o)
        result[c["id"]] = merged
    return result


@router.post("")
async def create_chart(
    request: Request,
    name: str = Form(...),
    domain: list[str] = Form([]),
    metric_key: list[str] = Form([]),
    param: list[str] = Form([]),
    normalize: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    series = [
        {"domain": d, "metric_key": mk, "param": (p.strip() or None)}
        for d, mk, p in zip(domain, metric_key, param)
        if mk
    ]
    try:
        await custom_charts_service.create_chart(
            db,
            name=name.strip(),
            series=series,
            normalize=normalize,
            redis=None,
            subject_id=ownership.subject_id,
        )
        await db.commit()
    except ChartConfigError as e:
        logger.warning("custom chart rejected: %s", e)
        return _redirect(request, "?error=invalid")
    charts = await custom_charts_service.list_charts(
        db,
        redis=None,
        subject_id=ownership.subject_id,
    )
    await custom_charts_service.prime_cache(
        redis,
        charts,
        subject_id=ownership.subject_id,
    )
    return _redirect(request)


@router.post("/{chart_id}/delete")
async def delete_chart_entry(
    request: Request,
    chart_id: str,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    await custom_charts_service.delete_chart(
        db,
        chart_id,
        redis=None,
        subject_id=ownership.subject_id,
    )
    await db.commit()
    charts = await custom_charts_service.list_charts(
        db,
        redis=None,
        subject_id=ownership.subject_id,
    )
    await custom_charts_service.prime_cache(
        redis,
        charts,
        subject_id=ownership.subject_id,
    )
    return _redirect(request)


def _redirect(request: Request, suffix: str = "") -> RedirectResponse:
    url = f"/charts{suffix}"
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = url
    return response
