"""``raw_payloads`` on a real Postgres — the JSONB behaviour SQLite only fakes.

The data lake's whole promise ("nothing is lost, we can always re-parse") rests on
``payload`` being real ``JSONB`` with a GIN index: containment (``@>``) and key
lookups have to work over arbitrary upstream shapes, and structure has to survive
the round-trip instead of degrading into a stringified blob. On the SQLite fast
path the column is generic ``JSON`` and ``@>`` doesn't exist at all, so these are
``@pytest.mark.integration``.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from vitals.enums import Domain, Source
from vitals.models.raw_payload import RawPayload

pytestmark = pytest.mark.integration


async def _seed(session, roots) -> None:
    """Three payloads belonging to this installation's owner.

    These tests are about JSONB — containment, key lookup, round-trip — not
    about ownership, but a payload always belongs to somebody now, so the
    seed says whose rather than asking for the older schema.
    """

    session.add_all(
        [
            RawPayload(
                subject_id=roots.subject_id,
                domain=Domain.GARMIN.value, source=Source.GARMIN_API.value, external_id="g1",
                payload={
                    "steps": 8000,
                    "sleep": {"score": 80, "stages": [{"stage": "deep", "secs": 5400}]},
                    "tags": ["auto", "watch"],
                },
            ),
            RawPayload(
                subject_id=roots.subject_id,
                domain=Domain.GARMIN.value, source=Source.GARMIN_API.value, external_id="g2",
                payload={"steps": 12000, "sleep": {"score": 62}},
            ),
            RawPayload(
                subject_id=roots.subject_id,
                domain=Domain.WORKOUTS.value, source=Source.HEVY_API.value, external_id="w1",
                payload={"title": "Push", "sets": [{"reps": 8, "weight_kg": 80.0}]},
            ),
        ]
    )
    await session.commit()


async def test_containment_query_finds_the_matching_payload(db_session, legacy_owner_roots):
    """``payload @> '{...}'`` — the query the GIN index exists for."""
    await _seed(db_session, legacy_owner_roots)

    rows = (
        await db_session.execute(
            select(RawPayload).where(RawPayload.payload.contains({"steps": 8000}))
        )
    ).scalars().all()
    assert [r.external_id for r in rows] == ["g1"]

    # Containment reaches into nested objects, not just top-level keys.
    nested = (
        await db_session.execute(
            select(RawPayload).where(RawPayload.payload.contains({"sleep": {"score": 62}}))
        )
    ).scalars().all()
    assert [r.external_id for r in nested] == ["g2"]


async def test_key_lookup_and_typed_extraction(db_session, legacy_owner_roots):
    """Re-parsing a stored payload means reading fields back out by path."""
    await _seed(db_session, legacy_owner_roots)

    row = (
        await db_session.execute(
            select(RawPayload).where(
                RawPayload.payload["sleep"]["score"].as_integer() == 80
            )
        )
    ).scalars().one()
    assert row.external_id == "g1"


async def test_structure_survives_the_roundtrip(db_session, legacy_owner_roots):
    """Nested lists/objects come back as structure — the whole point of storing
    the verbatim upstream response."""
    await _seed(db_session, legacy_owner_roots)
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(RawPayload).where(RawPayload.external_id == "g1")
        )
    ).scalars().one()
    assert row.payload["sleep"]["stages"][0]["stage"] == "deep"
    assert row.payload["tags"] == ["auto", "watch"]
    assert isinstance(row.payload["steps"], int)


async def test_unparsed_text_payload_is_storable(db_session, legacy_owner_roots):
    """An unreadable model answer is parked as ``{"_unparsed": raw}``;
    JSONB must accept that shape and hand the text back intact."""
    raw = 'Не JSON: модель ответила текстом {"почти": '
    db_session.add(
        RawPayload(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.LABS.value, source=Source.LAB_PARSER.value,
            external_id="unparsed-1", payload={"_unparsed": raw},
        )
    )
    await db_session.commit()
    db_session.expire_all()

    row = (
        await db_session.execute(
            select(RawPayload).where(RawPayload.payload.has_key("_unparsed"))
        )
    ).scalars().one()
    assert row.payload["_unparsed"] == raw
