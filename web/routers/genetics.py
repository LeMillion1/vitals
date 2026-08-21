"""Endpoints for the genetics reference table."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Source
from vitals.services import alerts_service, conflict_engine, genetics_service
from vitals.services.genetics_vcf import INTERPRETATIONS, ParsedVariant, parse_vcf_line
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.templating import templates
from web.uploads import VCF_EXTS, VCF_MAX_BYTES, iter_lines_capped, validate_extension

router = APIRouter(prefix="/genetics", tags=["genetics"])


async def _prepared_owner_write(
    db: AsyncSession,
    *,
    username: str,
):
    context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    prepared = await conflict_engine.prepare_scoped_write(
        db,
        context=context,
    )
    return context, prepared


def _redirect(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/genetics", status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = "/genetics"
    return response


@router.get("", response_class=HTMLResponse)
async def genetics_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    variants = await genetics_service.list_variants(
        db,
        subject_id=context.identity.subject_id,
    )
    alerts = await alerts_service.list_active_scoped(
        db,
        context=alerts_service.HealthAlertContext(context.identity),
        domain=Domain.GENETICS,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )
    return templates.TemplateResponse(
        request,
        "genetics/index.html",
        {
            "username": username,
            "variants": variants,
            "alerts": alerts,
            # Transient import summary (?imported=&markers=), shown as a banner.
            "imported": request.query_params.get("imported"),
            "imported_markers": request.query_params.get("markers"),
        },
    )


@router.post("/import")
async def import_vcf(
    request: Request,
    file: UploadFile = File(...),
    only_interpreted: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Parse an uploaded ``.vcf`` and upsert the **curated** variants (those in
    ``INTERPRETATIONS``), keyed by rsID. A full consumer genome is ~600k lines;
    upserting every raw variant would hang the request (Cloudflare 524), and raw
    unknown rows aren't useful as catalog entries — so we keep only the rsIDs we
    interpret (~dozens) as ``GeneticVariant`` rows. ``raw_payloads`` retains the
    first 50k parsed rows plus an explicit truncation flag for bounded replay;
    ``only_interpreted`` narrows the normalized catalog further to marker-bearing
    variants.

    Lines are membership-checked before any DB work, so even a large file does at
    most a few dozen upserts. The upload is exhausted before governance locks are
    taken, while retained raw rows remain capped for bounded memory use."""
    validate_extension(file.filename, VCF_EXTS)

    # Consume and parse the complete capped upload before taking the governance
    # lock. A slow client or a large VCF must never hold subject-write locks while
    # bytes are still arriving from the network.
    raw_variants: list[ParsedVariant] = []
    curated_variants: list[ParsedVariant] = []
    truncated = False
    async for line in iter_lines_capped(file, max_bytes=VCF_MAX_BYTES):
        variant = parse_vcf_line(line)
        if variant is None:
            continue
        if len(raw_variants) < genetics_service.MAX_RAW_VARIANTS:
            raw_variants.append(variant)
        else:
            truncated = True
        # Curated rows remain tiny and must be collected through EOF even when
        # the bounded raw sample filled near the start of a consumer genome.
        if variant.rsid in INTERPRETATIONS:
            curated_variants.append(variant)

    context, prepared = await _prepared_owner_write(db, username=username)
    try:
        summary = await genetics_service.ingest_vcf_batch(
            db,
            filename=file.filename,
            curated_variants=curated_variants,
            raw_variants=raw_variants,
            truncated=truncated,
            only_interpreted=only_interpreted,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return RedirectResponse(
        url=f"/genetics?imported={summary.imported}&markers={summary.markers}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/save")
async def save_variant(
    request: Request,
    gene: str = Form(...),
    rsid: Optional[str] = Form(None),
    genotype: Optional[str] = Form(None),
    marker: Optional[str] = Form(None),
    impact: Optional[str] = Form(None),
    impact_domain: Optional[str] = Form(None),
    interpretation: Optional[str] = Form(None),
    action_notes: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    context, prepared = await _prepared_owner_write(db, username=username)
    fields = {
        "gene": gene,
        "genotype": genotype or None,
        "marker": marker or None,
        "impact": impact or None,
        "impact_domain": impact_domain or None,
        "interpretation": interpretation or None,
        "action_notes": action_notes or None,
        "source": Source.MANUAL.value,
        "identity": context.identity,
        "prepared_conflict_write": prepared,
    }
    try:
        if rsid:
            # An rsID is a globally-unique dbSNP id: re-saving the same one
            # updates only the exact owner's row under the locked write boundary.
            await genetics_service.upsert_by_rsid(
                db,
                rsid=rsid,
                **fields,
            )
        else:
            await genetics_service.add_variant(
                db,
                rsid=None,
                **fields,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return _redirect(request)


@router.post("/{id}/delete")
async def delete_variant(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    context, prepared = await _prepared_owner_write(db, username=username)
    try:
        await genetics_service.delete_variant(
            db,
            id,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return _redirect(request)
