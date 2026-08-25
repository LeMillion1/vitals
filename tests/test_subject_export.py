"""What leaves in a personal export, and what must not go with it.

``export_full`` answers "what is in this installation" and is an operator's
file. ``export_subject`` answers "what is mine". The difference is not only a
``WHERE`` clause, and the parts that are not the clause are the parts worth
pinning: an export that quietly carried the installation's configuration would
be a way to reconfigure whatever imported it, and one that carried the curated
safety catalog would be a way to overwrite somebody else's.
"""

from __future__ import annotations

import pytest

from vitals.enums import Domain, Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.services import data_portability_service as portability


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


async def _supplement(session, subject_id, name: str, key: str):
    from vitals.models.supplements import Supplement

    row = Supplement(
        subject_id=subject_id,
        domain=Domain.SUPPLEMENTS.value,
        source=Source.MANUAL.value,
        name=name,
        key=key,
        active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def test_an_export_carries_one_subject_and_no_other(db_session):
    """The clause, stated as the property it is there for."""

    mine = await _subject(db_session, "export-mine")
    theirs = await _subject(db_session, "export-theirs")
    await _supplement(db_session, mine.id, "Mine", "export-mine-key")
    await _supplement(db_session, theirs.id, "Theirs", "export-theirs-key")

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)

    names = {row["name"] for row in snapshot["supplements"]}
    assert names == {"Mine"}
    assert "Theirs" not in str(snapshot)


async def test_an_export_leaves_the_installations_configuration_behind(db_session):
    """``app_settings`` is the deployment's, not the person's.

    It carries the timezone the scheduler runs on and which modules the
    installation has switched on. Carrying it in a personal file would make the
    file a way to reconfigure whatever imported it.
    """

    from vitals.models.app_settings import AppSetting

    mine = await _subject(db_session, "export-config")
    db_session.add(AppSetting(key="timezone", value="Asia/Almaty"))
    await db_session.flush()

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    assert "app_settings" not in snapshot
    assert "Asia/Almaty" not in str(
        {k: v for k, v in snapshot.items() if k != "metadata"}
    )


async def test_an_export_leaves_the_curated_catalog_behind(db_session):
    """A NULL subject means the installation's, and the receiver has its own.

    The safety catalog is seeded by migrations on every installation. Shipping a
    copy inside a personal export would let one person's file overwrite another
    installation's rules — which is the one table where being wrong is not a
    display bug.
    """

    from vitals.models.conflict_rule import ConflictRule

    mine = await _subject(db_session, "export-catalog")
    # The fast suite builds its schema with create_all, so the catalog the
    # migrations seed is not here; the shape is what matters, not the source.
    for subject_id, message in ((None, "the installation's rule"), (mine.id, "my own rule")):
        db_session.add(
            ConflictRule(
                subject_id=subject_id,
                rule_type="hard_block",
                domain_a=Domain.GENETICS.value,
                condition_a={},
                domain_b=Domain.SUPPLEMENTS.value,
                condition_b={},
                severity="block",
                message=message,
                active=True,
            )
        )
    await db_session.flush()

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    messages = {row["message"] for row in snapshot["conflict_rules"]}
    assert messages == {"my own rule"}


async def test_an_export_carries_no_ownership_or_storage_columns(db_session):
    """Assigned by a trusted boundary on the way in, never read from a file."""

    mine = await _subject(db_session, "export-columns")
    await _supplement(db_session, mine.id, "Column probe", "export-columns-key")

    snapshot = await portability.export_subject(db_session, subject_id=mine.id)
    for table_name, rows in snapshot.items():
        if table_name == "metadata":
            continue
        for row in rows:
            leaked = (
                set(row) & portability.GENERIC_OUTPUT_SUPPRESSED_COLUMNS
            )
            assert not leaked, f"{table_name} exported {sorted(leaked)}"


async def test_an_export_without_a_subject_is_refused(db_session):
    with pytest.raises(portability.PortabilityError):
        await portability.export_subject(db_session, subject_id=None)


async def test_a_personal_export_is_not_a_backup_the_importer_will_eat(db_session):
    """The mistake this guard exists for is plausible and the blast radius is not.

    ``import_full`` replaces every portable table for everybody. A personal
    export is valid JSON with the same envelope and overlapping table names, so
    without the kind check it would load — emptying the database and putting one
    person back into the hole.
    """

    mine = await _subject(db_session, "export-kind")
    await _supplement(db_session, mine.id, "Kind probe", "export-kind-key")
    snapshot = await portability.export_subject(db_session, subject_id=mine.id)

    assert snapshot["metadata"]["kind"] == portability.KIND_SUBJECT
    with pytest.raises(portability.PortabilityError, match="personal export"):
        await portability.import_full(db_session, snapshot)


async def test_a_full_backup_is_still_accepted(db_session, legacy_owner_roots):
    """The guard names one kind; it must not have closed the other."""

    snapshot = await portability.export_full(db_session)
    assert snapshot["metadata"]["kind"] == portability.KIND_FULL
    await portability.import_full(db_session, snapshot)


async def test_the_route_hands_back_only_the_callers_record(
    auth_client, db_session, legacy_owner_roots
):
    import json

    await _supplement(
        db_session, legacy_owner_roots.subject_id, "Route probe", "route-key"
    )
    await db_session.commit()

    response = await auth_client.get("/settings/export-subject")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"

    body = json.loads(response.content)
    assert body["metadata"]["kind"] == portability.KIND_SUBJECT
    assert {row["name"] for row in body["supplements"]} == {"Route probe"}
    assert "app_settings" not in body


async def test_the_full_backup_refuses_a_shared_installation_in_words(
    auth_client, db_session, legacy_owner_roots
):
    """A second person turns the operator's backup into a 409, not a crash.

    Format v1 describes an installation holding one person, so in a shared one
    it has nothing honest to write. That is a fact to state, together with the
    export that does work — it used to arrive as an unhandled ``PortabilityError``
    and therefore a 500 with a stack trace, which reads as a bug rather than as
    a limit of the format.
    """

    await _subject(db_session, "second-person")
    await db_session.commit()

    response = await auth_client.get("/settings/export")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "v1" in detail
    # Naming the alternative is the point: a refusal that leaves the reader with
    # nowhere to go is only half an answer.
    assert "export" in detail.lower()

    # And the export that is about one person still works next to it.
    assert (await auth_client.get("/settings/export-subject")).status_code == 200


async def test_the_multi_subject_refusal_is_its_own_type(
    db_session, legacy_owner_roots
):
    """Told apart from every other portability failure, and by type.

    Everything else this raises is about the file and is answered by fixing the
    file. This one is about the installation, the file is fine, and the answer
    is a different export — a distinction a router cannot make from a translated
    string.
    """

    await _subject(db_session, "another-person")
    await db_session.commit()

    with pytest.raises(portability.MultiSubjectBackupError):
        await portability.export_full(db_session)

    assert issubclass(
        portability.MultiSubjectBackupError, portability.PortabilityError
    )
