"""Restoring one person's record without touching anybody else's.

``import_full`` empties every portable table and reloads it, which is right for
a whole-database backup and catastrophic per person: restoring one record would
take the installation down to do it. ``import_subject`` scopes the delete.

Two consequences of that scoping are the substance of this file.

Primary keys cannot survive. Every portable table numbers its rows with an
integer sequence, so one subject's row 5 and another's row 5 both exist; keeping
ids would collide with rows the operation is not allowed to touch. Rows are
inserted fresh and the references between them are rewritten as each parent
lands.

And a reference can point out of the file. The installation's shared catalog
lives under a NULL subject and a personal export does not carry it — the
receiving installation seeded its own, numbered its own way. Those references
travel as a natural key. One that does not resolve is refused, because a dose
that quietly forgot which compound it was is worse than an import that did not
happen.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from vitals.enums import Domain, Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.services.portability import v1_contract, v1_export, v1_import

portability = SimpleNamespace(
    CATALOG_NATURAL_KEYS=v1_export.CATALOG_NATURAL_KEYS,
    KIND_SUBJECT=v1_export.KIND_SUBJECT,
    PORTABLE_REFERENCES=v1_export.PORTABLE_REFERENCES,
    PortabilityError=v1_contract.PortabilityError,
    export_full=v1_export.export_full,
    export_subject=v1_export.export_subject,
    import_subject=v1_import.import_subject,
)


async def _subject(session, slug: str) -> HealthSubject:
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
    return subject


async def _weight(session, subject_id, on_date: date, kg: float):
    from vitals.models.weight import WeightLog

    row = WeightLog(
        subject_id=subject_id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        date=on_date,
        weight_kg=kg,
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_restore_replaces_one_subject_and_leaves_the_other_alone(db_session):
    """The property the whole operation exists for."""

    from vitals.models.weight import WeightLog

    mine = await _subject(db_session, "import-mine")
    theirs = await _subject(db_session, "import-theirs")
    await _weight(db_session, mine.id, date(2026, 8, 1), 80.0)
    await _weight(db_session, theirs.id, date(2026, 8, 1), 65.0)

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)

    # Move on: a later weigh-in that the file does not know about.
    await _weight(db_session, mine.id, date(2026, 8, 20), 78.0)
    await db_session.flush()

    await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    mine_rows = (
        await db_session.scalars(
            select(WeightLog).where(WeightLog.subject_id == mine.id)
        )
    ).all()
    assert [row.weight_kg for row in mine_rows] == [80.0]

    theirs_rows = (
        await db_session.scalars(
            select(WeightLog).where(WeightLog.subject_id == theirs.id)
        )
    ).all()
    assert [row.weight_kg for row in theirs_rows] == [65.0]


async def test_the_ids_in_the_file_are_not_authoritative(db_session):
    """A file's id may be somebody else's row by the time it is restored.

    That is what forces the renumbering: the id space is shared across subjects,
    and the restore is only allowed to touch one of them. Here the id the file
    carries is handed to the other subject before the import runs, so preserving
    it would mean either failing on the primary key or overwriting a row this
    operation has no business touching.
    """

    from vitals.models.weight import WeightLog

    mine = await _subject(db_session, "import-ids-mine")
    theirs = await _subject(db_session, "import-ids-theirs")
    original = await _weight(db_session, mine.id, date(2026, 8, 4), 80.0)
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    contested_id = snapshot["weight_logs"][0]["id"]
    assert contested_id == original.id

    # The row the file names is now the other subject's.
    await db_session.delete(original)
    await db_session.flush()
    db_session.add(
        WeightLog(
            id=contested_id,
            subject_id=theirs.id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=date(2026, 8, 4),
            weight_kg=65.0,
        )
    )
    await db_session.flush()

    await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    restored = (
        await db_session.scalars(
            select(WeightLog).where(WeightLog.subject_id == mine.id)
        )
    ).all()
    assert [row.weight_kg for row in restored] == [80.0]
    assert restored[0].id != contested_id

    # The contested row is still the other subject's, and still theirs.
    contested = await db_session.get(WeightLog, contested_id)
    assert contested is not None
    assert contested.subject_id == theirs.id
    assert contested.weight_kg == 65.0


async def test_a_reference_inside_the_file_survives_the_renumbering(db_session):
    """Parent and child are renumbered together or the child points at nothing."""

    from vitals.models.raw_payload import RawPayload
    from vitals.models.weight import WeightLog

    mine = await _subject(db_session, "import-refs")
    raw = RawPayload(
        subject_id=mine.id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        external_id="synthetic-import-ref",
        payload={"kg": 80.0},
    )
    db_session.add(raw)
    await db_session.flush()

    row = await _weight(db_session, mine.id, date(2026, 8, 5), 80.0)
    row.raw_payload_id = raw.id
    await db_session.flush()

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    restored = (
        await db_session.scalars(
            select(WeightLog).where(WeightLog.subject_id == mine.id)
        )
    ).one()
    restored_raw = (
        await db_session.scalars(
            select(RawPayload).where(RawPayload.subject_id == mine.id)
        )
    ).one()
    assert restored.raw_payload_id == restored_raw.id
    assert restored_raw.external_id == "synthetic-import-ref"


# ── References that leave the file ───────────────────────────────────────────


async def _compound(session, *, subject_id, key: str, name: str):
    from vitals.models.hrt import HrtCompound

    row = HrtCompound(
        subject_id=subject_id,
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        key=key,
        name=name,
        compound_class="androgen",
        route="im",
        dose_unit="mg",
        active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def _dose(session, *, subject_id, compound_id, on_date: date):
    from vitals.models.hrt import HrtDose

    row = HrtDose(
        subject_id=subject_id,
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        compound_id=compound_id,
        compound_key="synthetic",
        date=on_date,
        dose=100.0,
        unit="mg",
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_reference_into_the_shared_catalog_travels_as_a_name(db_session):
    """An id would be meaningless in the installation that receives the file.

    The catalog is seeded by each installation's own migrations and numbered its
    own way, so the integer either dangles or — worse — lands on an unrelated
    row that happens to hold that number.
    """

    mine = await _subject(db_session, "import-catalog")
    curated = await _compound(
        db_session, subject_id=None, key="test-enanthate", name="Testosterone E"
    )
    await _dose(
        db_session, subject_id=mine.id, compound_id=curated.id, on_date=date(2026, 8, 6)
    )

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    exported = snapshot["hrt_doses"][0]
    assert exported["compound_id"] is None
    assert exported["_vitals_refs"]["compound_id"] == {
        "table": "hrt_compounds",
        "key": "test-enanthate",
    }
    # The catalog row itself is not in the file; it is not this person's.
    assert snapshot["hrt_compounds"] == []


async def test_the_name_is_resolved_against_the_receiving_installation(db_session):
    """Which is the point: a different id there, and the same compound."""

    from vitals.models.hrt import HrtDose

    mine = await _subject(db_session, "import-catalog-resolve")
    curated = await _compound(
        db_session, subject_id=None, key="test-cyp", name="Testosterone C"
    )
    await _dose(
        db_session, subject_id=mine.id, compound_id=curated.id, on_date=date(2026, 8, 7)
    )
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)

    await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    restored = (
        await db_session.scalars(
            select(HrtDose).where(HrtDose.subject_id == mine.id)
        )
    ).one()
    assert restored.compound_id == curated.id


async def test_an_unresolvable_reference_is_refused_rather_than_dropped(db_session):
    """A dose that quietly forgot its compound is worse than a failed import."""

    mine = await _subject(db_session, "import-catalog-missing")
    curated = await _compound(
        db_session, subject_id=None, key="gone-compound", name="Gone"
    )
    await _dose(
        db_session, subject_id=mine.id, compound_id=curated.id, on_date=date(2026, 8, 8)
    )
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)

    # The receiving installation does not have it.
    await db_session.delete(curated)
    await db_session.flush()

    with pytest.raises(portability.PortabilityError, match="gone-compound"):
        await portability.import_subject(db_session, snapshot, subject_id=mine.id)


async def test_the_catalog_row_wins_over_a_personal_row_with_the_same_name(db_session):
    """A travelling name always came from the installation's catalog.

    A reference to the subject's *own* compound never leaves the file — the
    export carries every row the subject owns, so that reference is renumbered
    by id like any other. Only NULL-subject targets travel as a name. Preferring
    a personal row here would therefore silently re-point a dose at a different
    compound than the one it recorded.
    """

    from vitals.models.hrt import HrtDose

    mine = await _subject(db_session, "import-catalog-shadow")
    curated = await _compound(
        db_session, subject_id=None, key="shadowed", name="Installation's"
    )
    await _dose(
        db_session, subject_id=mine.id, compound_id=curated.id, on_date=date(2026, 8, 9)
    )
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)

    await _compound(db_session, subject_id=mine.id, key="shadowed", name="Mine")
    await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    restored = (
        await db_session.scalars(
            select(HrtDose).where(HrtDose.subject_id == mine.id)
        )
    ).one()
    assert restored.compound_id == curated.id


async def test_a_personal_row_answers_only_when_the_catalog_has_nothing(db_session):
    """The cross-installation case, and what the scoped delete does to it.

    The receiver organises its catalog its own way and does not have the name
    the file carries. The fallback is the subject's own row of that name — but
    only one the file itself restored: the scoped delete removes the subject's
    rows before anything is loaded, so a personal compound that existed
    beforehand and is not in the file is gone by the time the reference is
    resolved. That is what a restore means, and it is why the fallback is
    reachable exactly when the file is self-consistent.
    """

    from vitals.models.hrt import HrtCompound, HrtDose

    mine = await _subject(db_session, "import-catalog-fallback")
    curated = await _compound(
        db_session, subject_id=None, key="portable-name", name="Installation's"
    )
    # The subject also keeps a compound of their own under that name, so the
    # file carries it as a row while the dose's reference travels as a name.
    await _compound(db_session, subject_id=mine.id, key="portable-name", name="Mine")
    await _dose(
        db_session,
        subject_id=mine.id,
        compound_id=curated.id,
        on_date=date(2026, 8, 12),
    )
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    assert len(snapshot["hrt_compounds"]) == 1

    # The receiving installation has no catalog entry by that name.
    await db_session.delete(curated)
    await db_session.flush()

    await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    restored = (
        await db_session.scalars(
            select(HrtDose).where(HrtDose.subject_id == mine.id)
        )
    ).one()
    reloaded = await db_session.get(HrtCompound, restored.compound_id)
    assert reloaded.subject_id == mine.id
    assert reloaded.key == "portable-name"


# ── What a file may not decide ───────────────────────────────────────────────


async def test_a_file_cannot_name_the_subject_it_lands_in(db_session):
    """Which is what stops one from landing in somebody else's record."""

    from vitals.models.weight import WeightLog

    mine = await _subject(db_session, "import-boundary-mine")
    theirs = await _subject(db_session, "import-boundary-theirs")
    await _weight(db_session, mine.id, date(2026, 8, 10), 80.0)

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    # Even said outright, in the shape the column expects.
    for row in snapshot["weight_logs"]:
        row["subject_id"] = str(theirs.id)

    await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    assert await db_session.scalar(
        select(func.count()).select_from(WeightLog).where(
            WeightLog.subject_id == theirs.id
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(WeightLog).where(
            WeightLog.subject_id == mine.id
        )
    ) == 1


async def test_a_whole_database_backup_is_refused_here(db_session, legacy_owner_roots):
    """Loading one would silently truncate it to one subject's worth of itself.

    Which looks like a successful restore and is not one — the mirror of
    ``import_full`` refusing a personal export.
    """

    backup = await portability.export_full(db_session)
    with pytest.raises(portability.PortabilityError, match="not a personal export"):
        await portability.import_subject(
            db_session, backup, subject_id=legacy_owner_roots.subject_id
        )


async def test_a_file_cannot_reach_a_table_outside_the_portable_set(db_session):
    mine = await _subject(db_session, "import-excluded")
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    snapshot["users"] = [{"username": "planted"}]

    with pytest.raises(portability.PortabilityError):
        await portability.import_subject(db_session, snapshot, subject_id=mine.id)


async def test_a_malformed_reference_descriptor_is_refused(db_session):
    mine = await _subject(db_session, "import-bad-descriptor")
    await _weight(db_session, mine.id, date(2026, 8, 11), 80.0)
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    snapshot["weight_logs"][0]["_vitals_refs"] = {
        "raw_payload_id": {"table": "users", "key": "planted"}
    }

    with pytest.raises(portability.PortabilityError):
        await portability.import_subject(db_session, snapshot, subject_id=mine.id)


@pytest.mark.parametrize(
    ("table_name", "required_column"),
    (
        ("garmin_daily", "integration_connection_id"),
        ("progress_photos", "file_asset_id"),
    ),
)
async def test_v1_rejects_unrestorable_required_roots_before_deleting_the_record(
    db_session, table_name, required_column
):
    """A crafted or older archive cannot turn a schema error into data loss."""

    from vitals.models.weight import WeightLog

    mine = await _subject(db_session, f"import-required-{table_name}")
    sentinel = await _weight(db_session, mine.id, date(2026, 8, 23), 80.0)
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    snapshot[table_name] = [{"id": 9001}]

    with pytest.raises(portability.PortabilityError, match=required_column):
        await portability.import_subject(db_session, snapshot, subject_id=mine.id)

    preserved = await db_session.scalar(
        select(WeightLog).where(
            WeightLog.id == sentinel.id,
            WeightLog.subject_id == mine.id,
        )
    )
    assert preserved is not None
    assert preserved.weight_kg == 80.0


@pytest.mark.parametrize(
    "table_name", ("garmin_daily", "progress_photos")
)
async def test_v1_unrestorable_archive_is_a_controlled_http_400(
    auth_client, table_name
):
    """The browser boundary must not leak the later NOT NULL failure as a 500."""

    import io
    import json

    payload = {
        "metadata": {"version": "1.0", "kind": portability.KIND_SUBJECT},
        table_name: [{"id": 9001}],
    }
    response = await auth_client.post(
        "/settings/import-subject",
        files={
            "backup_file": (
                "record.json",
                io.BytesIO(json.dumps(payload).encode()),
                "application/json",
            )
        },
    )

    assert response.status_code == 400


async def test_the_reference_map_is_derived_from_the_schema(db_session):
    """A reference added later must not silently become a portable local id."""

    from vitals.models.base import Base

    for table_name, columns in portability.PORTABLE_REFERENCES.items():
        table = Base.metadata.tables[table_name]
        for column, target in columns.items():
            assert column in table.columns
            assert column != "subject_id"
            assert "subject_id" in Base.metadata.tables[target].columns


def test_every_reference_that_can_leave_the_file_has_a_natural_key():
    """Otherwise the export refuses at the door, which is a worse place to find out.

    A reference points out of a personal export exactly when its target may live
    under a NULL subject — the installation's shared catalog. Those targets need
    a name the receiving installation can resolve.
    """

    from vitals.ownership import OWNERSHIP_REGISTRY, TargetColumn

    may_leave = {
        target
        for columns in portability.PORTABLE_REFERENCES.values()
        for target in columns.values()
        if OWNERSHIP_REGISTRY[target].subject
        in {TargetColumn.MIXED, TargetColumn.OPTIONAL}
    }
    missing = sorted(may_leave - set(portability.CATALOG_NATURAL_KEYS))
    assert not missing, (
        f"referenced tables whose rows can belong to the installation, with no "
        f"portable name: {missing} — give each one a natural key, or the export "
        "refuses every record that uses them"
    )


# ── The route ────────────────────────────────────────────────────────────────


async def test_the_route_restores_the_callers_own_record(
    auth_client, db_session, legacy_owner_roots
):
    """Authorized like an export, not like an operator's restore.

    It deletes and reloads exactly the caller's subject, which is theirs to do —
    unlike ``/settings/import``, which empties every portable table and is
    reserved.
    """

    import io
    import json

    from vitals.models.weight import WeightLog

    await _weight(
        db_session, legacy_owner_roots.subject_id, date(2026, 8, 13), 80.0
    )
    await db_session.commit()

    snapshot = await portability.export_subject(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    await _weight(
        db_session, legacy_owner_roots.subject_id, date(2026, 8, 14), 78.0
    )
    await db_session.commit()

    body = json.dumps(snapshot, default=str).encode()
    response = await auth_client.post(
        "/settings/import-subject",
        files={
            "backup_file": ("record.json", io.BytesIO(body), "application/json")
        },
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    rows = (
        await db_session.scalars(
            select(WeightLog).where(
                WeightLog.subject_id == legacy_owner_roots.subject_id
            )
        )
    ).all()
    assert [row.weight_kg for row in rows] == [80.0]


async def test_the_route_refuses_a_whole_database_backup(
    auth_client, db_session, legacy_owner_roots
):
    """Loading one here would truncate it to one subject and look like success."""

    import io
    import json

    backup = await portability.export_full(db_session)
    body = json.dumps(backup, default=str).encode()
    response = await auth_client.post(
        "/settings/import-subject",
        files={
            "backup_file": ("backup.json", io.BytesIO(body), "application/json")
        },
    )
    assert response.status_code == 400
    # Rendered in the caller's language, so assert the fact rather than the words.
    assert "экспорт" in response.text or "personal export" in response.text
