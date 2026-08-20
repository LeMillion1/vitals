"""Stage-2 ownership and raw-first boundaries for Genetics."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Source, UserStatus
from vitals.models.genetics import GeneticVariant
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, genetics_service
from vitals.services.genetics_vcf import ParsedVariant
from vitals.utils.timeutils import now_local


RISK = ParsedVariant("rs1800562", "G", "A", "G/A")
REFERENCE = ParsedVariant("rs1800562", "G", "A", "G/G")
MTHFR = ParsedVariant("rs1801133", "C", "T", "C/T")


def _identity(roots, *, system: bool = False) -> WriteIdentity:
    return WriteIdentity(
        roots.subject_id,
        None if system else roots.user_id,
    )


async def _prepared(
    session,
    identity: WriteIdentity,
    *,
    legacy: bool = False,
):
    return await conflict_engine.prepare_scoped_write(
        session,
        context=conflict_engine.ConflictWriteContext(
            identity=identity,
            evaluation_date=conflict_engine.today_local(),
            legacy_bridge=(
                conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
                if legacy
                else conflict_engine.LegacyConflictBridge.REJECT
            ),
        ),
    )


async def _second_owner(session, slug: str = "genetics-b"):
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return user, subject, WriteIdentity(subject.id, user.id)


async def _ingest(
    session,
    identity: WriteIdentity,
    prepared,
    variant: ParsedVariant,
    *,
    filename: str = "synthetic.vcf",
    only_interpreted: bool = False,
    legacy: bool = False,
):
    return await genetics_service.ingest_vcf_batch(
        session,
        filename=filename,
        raw_variants=[variant],
        curated_variants=[variant],
        truncated=False,
        only_interpreted=only_interpreted,
        identity=identity,
        prepared_conflict_write=prepared,
        include_legacy_unowned=legacy,
    )


async def test_scoped_manual_write_and_list_stamp_owner(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity)

    row = await genetics_service.add_variant(
        db_session,
        gene="COMT",
        rsid="rs4680",
        genotype="A/A",
        marker="comt_slow_metabolizer",
        source=Source.MANUAL.value,
        identity=identity,
        prepared_conflict_write=prepared,
    )

    assert row.subject_id == identity.subject_id
    assert row.actor_user_id == identity.actor_user_id
    assert row.source == Source.MANUAL.value
    assert row.raw_payload_id is None
    assert [item.id for item in await genetics_service.list_variants(
        db_session,
        subject_id=identity.subject_id,
    )] == [row.id]


async def test_rsid_case_normalization_prevents_duplicate_facts(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity)
    first = await genetics_service.upsert_by_rsid(
        db_session,
        gene="HFE",
        rsid="RS1800562",
        genotype="G/A",
        source=Source.MANUAL.value,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    second = await genetics_service.upsert_by_rsid(
        db_session,
        gene="HFE",
        rsid="rs1800562",
        genotype="G/G",
        source=Source.MANUAL.value,
        identity=identity,
        prepared_conflict_write=prepared,
    )

    rows = list(await db_session.scalars(select(GeneticVariant)))
    assert second.id == first.id
    assert len(rows) == 1
    assert rows[0].rsid == "rs1800562"
    assert rows[0].genotype == "G/G"


async def test_partial_actor_root_fails_closed(
    db_session,
    legacy_owner_roots,
):
    foreign_user, _, _ = await _second_owner(db_session)
    db_session.add(
        GeneticVariant(
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=foreign_user.id,
            domain="genetics",
            source=Source.MANUAL.value,
            gene="FORGED",
            rsid="rs-forged-actor",
        )
    )
    await db_session.flush()

    with pytest.raises(genetics_service.GeneticsOwnershipError):
        await genetics_service.list_variants(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            include_legacy_unowned=True,
        )


async def test_partial_legacy_fact_fails_closed_in_conflict_resolver(
    db_session,
    legacy_owner_roots,
):
    db_session.add(
        GeneticVariant(
            actor_user_id=legacy_owner_roots.user_id,
            domain="genetics",
            source=Source.MANUAL.value,
            gene="FORGED",
            rsid="rs-partial-conflict",
            marker="hemochromatosis_carrier",
        )
    )
    await db_session.flush()

    with pytest.raises(genetics_service.GeneticsOwnershipError, match="partial"):
        await genetics_service.resolve_variants_scoped(
            db_session,
            scope=conflict_engine.ConflictScope(
                subject_id=legacy_owner_roots.subject_id,
                evaluation_date=conflict_engine.today_local(),
                legacy_bridge=conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            ),
        )


async def test_foreign_id_is_non_enumerating_and_global_rsid_collision_is_typed(
    db_session,
    legacy_owner_roots,
):
    _, _, foreign_identity = await _second_owner(db_session)
    foreign = GeneticVariant(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        domain="genetics",
        source=Source.MANUAL.value,
        gene="HFE",
        rsid="rs1800562",
    )
    db_session.add(foreign)
    await db_session.flush()

    owner_identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, owner_identity)
    assert await genetics_service.get_variant(
        db_session,
        foreign.id,
        subject_id=owner_identity.subject_id,
    ) is None
    assert not await genetics_service.delete_variant(
        db_session,
        foreign.id,
        identity=owner_identity,
        prepared_conflict_write=prepared,
    )
    with pytest.raises(
        genetics_service.GeneticsScopedUniqueCutoverRequiredError
    ):
        await genetics_service.upsert_by_rsid(
            db_session,
            gene="HFE",
            rsid="rs1800562",
            genotype="G/A",
            source=Source.MANUAL.value,
            identity=owner_identity,
            prepared_conflict_write=prepared,
        )


async def test_vcf_batch_is_owned_raw_first_and_reimport_clears_stale_marker(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity, legacy=True)
    first = await _ingest(db_session, identity, prepared, RISK, legacy=True)
    assert first.raw is not None
    row = await db_session.scalar(
        select(GeneticVariant).where(GeneticVariant.rsid == RISK.rsid)
    )
    assert row is not None
    assert row.marker == "hemochromatosis_carrier"
    assert row.subject_id == identity.subject_id
    assert row.actor_user_id == identity.actor_user_id
    assert row.raw_payload_id == first.raw.id
    assert first.raw.subject_id == identity.subject_id
    assert first.raw.actor_user_id == identity.actor_user_id
    assert first.raw.integration_connection_id is None
    assert first.raw.file_asset_id is None
    assert first.raw.processed_at is not None

    second = await _ingest(
        db_session,
        identity,
        prepared,
        REFERENCE,
        only_interpreted=True,
        legacy=True,
    )
    assert second.raw is not None
    await db_session.refresh(row)
    assert row.genotype == "G/G"
    assert row.marker is None


async def test_vcf_reimport_from_new_file_relinks_corrected_fact_to_new_raw(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity, legacy=True)
    first = await _ingest(
        db_session,
        identity,
        prepared,
        RISK,
        filename="first-genome.vcf",
        legacy=True,
    )
    second = await _ingest(
        db_session,
        identity,
        prepared,
        REFERENCE,
        filename="replacement-genome.vcf",
        legacy=True,
    )
    assert first.raw is not None
    assert second.raw is not None
    assert second.raw.id != first.raw.id

    row = await db_session.scalar(
        select(GeneticVariant).where(GeneticVariant.rsid == RISK.rsid)
    )
    assert row is not None
    assert row.genotype == REFERENCE.genotype
    assert row.raw_payload_id == second.raw.id
    assert row.source == Source.VCF_IMPORT.value


async def test_same_filename_reimport_preserves_prior_raw_evidence(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity, legacy=True)
    first = await genetics_service.ingest_vcf_batch(
        db_session,
        filename="reused-name.vcf",
        raw_variants=[RISK, MTHFR],
        curated_variants=[RISK, MTHFR],
        truncated=False,
        identity=identity,
        prepared_conflict_write=prepared,
        include_legacy_unowned=True,
    )
    second = await _ingest(
        db_session,
        identity,
        prepared,
        REFERENCE,
        filename="reused-name.vcf",
        legacy=True,
    )

    assert first.raw is not None
    assert second.raw is not None
    assert first.raw.id != second.raw.id
    assert first.raw.external_id.startswith("vcf:")
    assert second.raw.external_id.startswith("vcf:")
    assert first.raw.payload["variants"] == [
        [RISK.rsid, RISK.ref, RISK.alt, RISK.genotype],
        [MTHFR.rsid, MTHFR.ref, MTHFR.alt, MTHFR.genotype],
    ]
    rows = {
        row.rsid: row
        for row in await genetics_service.list_variants(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )
    }
    assert rows[RISK.rsid].raw_payload_id == second.raw.id
    assert rows[RISK.rsid].marker is None
    assert rows[MTHFR.rsid].raw_payload_id == first.raw.id
    assert rows[MTHFR.rsid].marker == "mthfr_heterozygous"


async def test_mcp_patch_preserves_vcf_origin_and_omitted_fields(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity, legacy=True)
    summary = await _ingest(db_session, identity, prepared, MTHFR, legacy=True)
    row = await genetics_service.upsert_by_rsid(
        db_session,
        gene="MTHFR",
        rsid=MTHFR.rsid,
        interpretation="human clarification",
        source=Source.MCP.value,
        identity=identity,
        prepared_conflict_write=prepared,
        include_legacy_unowned=True,
    )

    assert row.source == Source.VCF_IMPORT.value
    assert row.actor_user_id == identity.actor_user_id
    assert row.raw_payload_id == summary.raw.id
    assert row.genotype == MTHFR.genotype
    assert row.marker == "mthfr_heterozygous"
    assert row.interpretation == "human clarification"


async def test_wrong_shape_raw_link_fails_before_read(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    connection = await db_session.scalar(select(IntegrationConnection).limit(1))
    assert connection is not None
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=connection.id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="forged.vcf",
        payload={
            "filename": "forged.vcf",
            "truncated": False,
            "variants": [[RISK.rsid, RISK.ref, RISK.alt, RISK.genotype]],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        GeneticVariant(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            domain="genetics",
            source=Source.VCF_IMPORT.value,
            raw_payload_id=raw.id,
            gene="HFE",
            rsid="rs-forged-raw",
        )
    )
    await db_session.flush()

    with pytest.raises(genetics_service.GeneticsRawProvenanceError):
        await genetics_service.list_variants(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )


async def test_manual_fact_cannot_use_raw_bridge_and_raw_flags_are_typed(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="typed.vcf",
        payload={
            "filename": "typed.vcf",
            "truncated": "false",
            "variants": [[RISK.rsid, RISK.ref, RISK.alt, RISK.genotype]],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    manual = GeneticVariant(
        domain="genetics",
        source=Source.MANUAL.value,
        raw_payload_id=raw.id,
        gene="HFE",
        rsid=RISK.rsid,
    )
    db_session.add(manual)
    await db_session.flush()

    with pytest.raises(
        genetics_service.GeneticsRawProvenanceError,
        match="manual and MCP",
    ):
        await genetics_service.list_variants(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )

    manual.source = Source.VCF_IMPORT.value
    with pytest.raises(
        genetics_service.GeneticsRawProvenanceError,
        match="truncation flag",
    ):
        await genetics_service.list_variants(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )


async def test_truncated_curated_hit_must_agree_with_retained_raw_evidence(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity)

    with pytest.raises(
        genetics_service.GeneticsRawProvenanceError,
        match="contradicts",
    ):
        await genetics_service.ingest_vcf_batch(
            db_session,
            filename="contradictory.vcf",
            raw_variants=[RISK],
            curated_variants=[REFERENCE],
            truncated=True,
            identity=identity,
            prepared_conflict_write=prepared,
        )

    assert list(await db_session.scalars(select(RawPayload))) == []
    assert list(await db_session.scalars(select(GeneticVariant))) == []


async def test_legacy_adoption_preserves_null_actor_and_exact_raw_bridge(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="legacy.vcf",
        payload={
            "filename": "legacy.vcf",
            "truncated": False,
            "variants": [[RISK.rsid, RISK.ref, RISK.alt, RISK.genotype]],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    legacy = GeneticVariant(
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        raw_payload_id=raw.id,
        gene="HFE",
        rsid=RISK.rsid,
        genotype=RISK.genotype,
    )
    db_session.add(legacy)
    await db_session.flush()
    prepared = await _prepared(db_session, identity, legacy=True)

    row = await genetics_service.upsert_by_rsid(
        db_session,
        gene="HFE",
        rsid=RISK.rsid,
        marker="hemochromatosis_carrier",
        source=Source.MCP.value,
        identity=identity,
        prepared_conflict_write=prepared,
        include_legacy_unowned=True,
    )
    assert row.subject_id == identity.subject_id
    assert row.actor_user_id is None
    assert row.raw_payload_id == raw.id
    assert await genetics_service.get_variant(
        db_session,
        row.id,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    ) is row


async def test_reimport_completes_fully_null_fact_without_overwriting_legacy_raw(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    raw = RawPayload(
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="adopted.vcf",
        payload={
            "filename": "adopted.vcf",
            "truncated": False,
            "only_interpreted": False,
            "variants": [[RISK.rsid, RISK.ref, RISK.alt, RISK.genotype]],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    legacy = GeneticVariant(
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        raw_payload_id=raw.id,
        gene="HFE",
        rsid=RISK.rsid,
        genotype=RISK.genotype,
    )
    db_session.add(legacy)
    await db_session.flush()
    prepared = await _prepared(db_session, identity, legacy=True)

    summary = await _ingest(
        db_session,
        identity,
        prepared,
        REFERENCE,
        filename="adopted.vcf",
        legacy=True,
    )

    assert summary.raw is not None
    assert summary.raw is not raw
    assert summary.raw.subject_id == identity.subject_id
    assert summary.raw.actor_user_id == identity.actor_user_id
    assert summary.raw.processed_at is not None
    assert raw.subject_id is None
    assert raw.actor_user_id is None
    assert raw.processed_at is None
    assert legacy.subject_id == identity.subject_id
    assert legacy.actor_user_id is None
    assert legacy.raw_payload_id == summary.raw.id
    assert legacy.genotype == REFERENCE.genotype
    assert legacy.marker is None


async def test_replay_fills_missing_curated_children_and_is_idempotent(
    db_session,
    legacy_owner_roots,
):
    owner = _identity(legacy_owner_roots)
    raw = RawPayload(
        subject_id=owner.subject_id,
        actor_user_id=owner.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="pending.vcf",
        payload={
            "filename": "pending.vcf",
            "truncated": False,
            "variants": [
                [RISK.rsid, RISK.ref, RISK.alt, RISK.genotype],
                [MTHFR.rsid, MTHFR.ref, MTHFR.alt, MTHFR.genotype],
            ],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        GeneticVariant(
            subject_id=owner.subject_id,
            actor_user_id=owner.actor_user_id,
            domain="genetics",
            source=Source.VCF_IMPORT.value,
            raw_payload_id=raw.id,
            gene="HFE",
            rsid=RISK.rsid,
            genotype=RISK.genotype,
            marker="hemochromatosis_carrier",
        )
    )
    await db_session.flush()

    system = WriteIdentity(owner.subject_id, None)
    prepared = await _prepared(db_session, system, legacy=True)
    assert await genetics_service.reparse_owned_pending(
        db_session,
        identity=system,
        prepared_conflict_write=prepared,
        include_legacy_unowned=True,
    ) == 1
    assert raw.processed_at is not None
    assert set(await db_session.scalars(select(GeneticVariant.rsid))) == {
        RISK.rsid,
        MTHFR.rsid,
    }
    assert await genetics_service.reparse_owned_pending(
        db_session,
        identity=system,
        prepared_conflict_write=prepared,
        include_legacy_unowned=True,
    ) == 0


async def test_replay_cannot_roll_fact_back_to_older_pending_raw(
    db_session,
    legacy_owner_roots,
):
    owner = _identity(legacy_owner_roots)
    old_raw = RawPayload(
        subject_id=owner.subject_id,
        actor_user_id=owner.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="old-reference.vcf",
        fetched_at=now_local() - timedelta(days=1),
        payload={
            "filename": "old-reference.vcf",
            "truncated": False,
            "only_interpreted": False,
            "variants": [
                [REFERENCE.rsid, REFERENCE.ref, REFERENCE.alt, REFERENCE.genotype]
            ],
        },
    )
    current_raw = RawPayload(
        subject_id=owner.subject_id,
        actor_user_id=owner.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="current-risk.vcf",
        fetched_at=now_local(),
        processed_at=now_local(),
        payload={
            "filename": "current-risk.vcf",
            "truncated": False,
            "only_interpreted": False,
            "variants": [[RISK.rsid, RISK.ref, RISK.alt, RISK.genotype]],
        },
    )
    db_session.add_all([old_raw, current_raw])
    await db_session.flush()
    fact = GeneticVariant(
        subject_id=owner.subject_id,
        actor_user_id=owner.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        raw_payload_id=current_raw.id,
        gene="HFE",
        rsid=RISK.rsid,
        genotype=RISK.genotype,
        marker="hemochromatosis_carrier",
    )
    db_session.add(fact)
    await db_session.flush()

    system = WriteIdentity(owner.subject_id, None)
    prepared = await _prepared(db_session, system, legacy=True)
    assert await genetics_service.reparse_owned_pending(
        db_session,
        identity=system,
        prepared_conflict_write=prepared,
        include_legacy_unowned=True,
    ) == 1
    assert old_raw.processed_at is not None
    assert fact.raw_payload_id == current_raw.id
    assert fact.genotype == RISK.genotype
    assert fact.marker == "hemochromatosis_carrier"


@pytest.mark.parametrize(
    ("only_interpreted", "expected_rows"),
    [(False, 1), (True, 0), (None, 0)],
    ids=["broad", "narrow", "legacy-missing-is-narrow"],
)
async def test_replay_uses_durable_only_interpreted_policy(
    db_session,
    legacy_owner_roots,
    only_interpreted,
    expected_rows,
):
    owner = _identity(legacy_owner_roots)
    payload = {
        "filename": "policy.vcf",
        "truncated": False,
        "variants": [
            [REFERENCE.rsid, REFERENCE.ref, REFERENCE.alt, REFERENCE.genotype]
        ],
    }
    if only_interpreted is not None:
        payload["only_interpreted"] = only_interpreted
    raw = RawPayload(
        subject_id=owner.subject_id,
        actor_user_id=owner.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="policy.vcf",
        payload=payload,
    )
    db_session.add(raw)
    await db_session.flush()

    system = WriteIdentity(owner.subject_id, None)
    prepared = await _prepared(db_session, system, legacy=True)
    assert await genetics_service.reparse_owned_pending(
        db_session,
        identity=system,
        prepared_conflict_write=prepared,
        include_legacy_unowned=True,
    ) == 1
    rows = list(await db_session.scalars(select(GeneticVariant)))
    assert len(rows) == expected_rows
    if rows:
        assert rows[0].rsid == REFERENCE.rsid
        assert rows[0].genotype == REFERENCE.genotype
        assert rows[0].marker is None
    assert raw.processed_at is not None


async def test_header_only_batch_is_write_free(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity)
    summary = await genetics_service.ingest_vcf_batch(
        db_session,
        filename="header-only.vcf",
        raw_variants=[],
        curated_variants=[],
        truncated=False,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    assert summary.raw is None
    assert summary.imported == 0
    assert list(await db_session.scalars(select(RawPayload))) == []


@pytest.mark.integration
async def test_postgres_concurrent_same_subject_rsid_upserts_serialize(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = _identity(legacy_owner_roots)
    await db_session.commit()
    writer_b_attempted = asyncio.Event()

    session_a = factory()
    prepared_a = await _prepared(session_a, identity)
    await genetics_service.upsert_by_rsid(
        session_a,
        gene="HFE",
        rsid=RISK.rsid,
        genotype="G/A",
        source=Source.MANUAL.value,
        identity=identity,
        prepared_conflict_write=prepared_a,
    )

    async def writer_b() -> None:
        async with factory() as session_b:
            writer_b_attempted.set()
            prepared_b = await _prepared(session_b, identity)
            await genetics_service.upsert_by_rsid(
                session_b,
                gene="HFE",
                rsid=RISK.rsid,
                genotype="G/G",
                source=Source.MCP.value,
                identity=identity,
                prepared_conflict_write=prepared_b,
            )
            await session_b.commit()

    task_b = asyncio.create_task(writer_b())
    await asyncio.wait_for(writer_b_attempted.wait(), timeout=5)
    writer_b_was_blocked = False
    try:
        await asyncio.wait_for(asyncio.shield(task_b), timeout=0.2)
    except asyncio.TimeoutError:
        writer_b_was_blocked = True
    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(GeneticVariant).where(
                    GeneticVariant.subject_id == identity.subject_id,
                    GeneticVariant.rsid == RISK.rsid,
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].genotype == "G/G"
    # The second correction changes content, not the original provenance.
    assert rows[0].source == Source.MANUAL.value
    assert writer_b_was_blocked


@pytest.mark.integration
async def test_postgres_legacy_write_holds_exact_one_governance_through_commit(
    db_session,
    legacy_owner_roots,
):
    from vitals.services.identity_service import acquire_identity_governance_lock

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await db_session.commit()
    bridge_locked = asyncio.Event()
    subject_write_attempted = asyncio.Event()
    release_legacy_write = asyncio.Event()

    async def legacy_write() -> None:
        async with factory() as session:
            context = await conflict_engine.resolve_legacy_conflict_write_context(
                session,
                actor_username="tester",
            )
            bridge_locked.set()
            await asyncio.wait_for(release_legacy_write.wait(), timeout=5)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=context,
            )
            await genetics_service.upsert_by_rsid(
                session,
                gene="HFE",
                rsid=RISK.rsid,
                genotype=RISK.genotype,
                source=Source.MANUAL.value,
                identity=context.identity,
                prepared_conflict_write=prepared,
                include_legacy_unowned=True,
            )
            await session.commit()

    async def create_second_subject() -> None:
        await asyncio.wait_for(bridge_locked.wait(), timeout=5)
        async with factory() as session:
            subject_write_attempted.set()
            await acquire_identity_governance_lock(session)
            await _second_owner(session, "genetics-governance-race")
            await session.commit()

    legacy_task = asyncio.create_task(legacy_write())
    subject_task = asyncio.create_task(create_second_subject())
    await asyncio.wait_for(subject_write_attempted.wait(), timeout=5)
    subject_write_was_blocked = False
    try:
        await asyncio.wait_for(asyncio.shield(subject_task), timeout=0.2)
    except asyncio.TimeoutError:
        subject_write_was_blocked = True
    release_legacy_write.set()
    await asyncio.wait_for(
        asyncio.gather(legacy_task, subject_task),
        timeout=10,
    )

    async with factory() as verify:
        subjects = list(await verify.scalars(select(HealthSubject.id)))
        row = await verify.scalar(
            select(GeneticVariant).where(GeneticVariant.rsid == RISK.rsid)
        )
    assert len(subjects) == 2
    assert row is not None
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.actor_user_id == legacy_owner_roots.user_id
    assert subject_write_was_blocked


@pytest.mark.integration
async def test_postgres_concurrent_owned_replay_claims_one_raw_exactly_once(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    human = _identity(legacy_owner_roots)
    system = _identity(legacy_owner_roots, system=True)
    raw = RawPayload(
        subject_id=human.subject_id,
        actor_user_id=human.actor_user_id,
        domain="genetics",
        source=Source.VCF_IMPORT.value,
        external_id="concurrent-replay.vcf",
        payload={
            "filename": "concurrent-replay.vcf",
            "truncated": False,
            "variants": [[MTHFR.rsid, MTHFR.ref, MTHFR.alt, MTHFR.genotype]],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    raw_id = raw.id
    await db_session.commit()

    first_replacement_started = asyncio.Event()
    release_first_replacement = asyncio.Event()
    worker_b_attempted = asyncio.Event()
    original_replace = genetics_service._replace_vcf_rows
    replacements = 0

    async def replacement_barrier(*args, **kwargs):
        nonlocal replacements
        replacements += 1
        if replacements == 1:
            first_replacement_started.set()
            await asyncio.wait_for(release_first_replacement.wait(), timeout=5)
        return await original_replace(*args, **kwargs)

    monkeypatch.setattr(genetics_service, "_replace_vcf_rows", replacement_barrier)

    async def worker(*, attempted: asyncio.Event | None = None) -> int:
        async with factory() as session:
            if attempted is not None:
                attempted.set()
            prepared = await _prepared(session, system, legacy=True)
            done = await genetics_service.reparse_owned_pending(
                session,
                identity=system,
                prepared_conflict_write=prepared,
                include_legacy_unowned=True,
            )
            await session.commit()
            return done

    worker_a = asyncio.create_task(worker())
    await asyncio.wait_for(first_replacement_started.wait(), timeout=5)
    worker_b = asyncio.create_task(worker(attempted=worker_b_attempted))
    await asyncio.wait_for(worker_b_attempted.wait(), timeout=5)
    worker_b_was_blocked = False
    try:
        await asyncio.wait_for(asyncio.shield(worker_b), timeout=0.2)
    except asyncio.TimeoutError:
        worker_b_was_blocked = True
    release_first_replacement.set()
    results = await asyncio.wait_for(
        asyncio.gather(worker_a, worker_b),
        timeout=10,
    )

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(GeneticVariant).where(GeneticVariant.raw_payload_id == raw_id)
            )
        )
        persisted_raw = await verify.get(RawPayload, raw_id)
    assert sorted(results) == [0, 1]
    assert replacements == 1
    assert len(rows) == 1
    assert rows[0].subject_id == human.subject_id
    assert rows[0].actor_user_id == human.actor_user_id
    assert persisted_raw is not None
    assert persisted_raw.processed_at is not None
    assert worker_b_was_blocked
