"""Security and usability contract for the encrypted personal-record routes."""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

from vitals.models.weight import WeightLog
from vitals.services.portability.archive_reader import open_validated_encrypted_archive
from vitals.services.portability.record_decoder import decode_validated_record
from web.auth import create_federated_session
from web.config import SESSION_COOKIE


PASSPHRASE = "synthetic protected record phrase"


async def _encrypted_export(auth_client) -> bytes:
    response = await auth_client.post(
        "/settings/portability-v2/export",
        data={"passphrase": PASSPHRASE},
    )
    assert response.status_code == 200, response.text
    return response.content


async def test_export_is_encrypted_private_and_immediately_inspectable(
    auth_client,
    legacy_owner_roots,
):
    body = await _encrypted_export(auth_client)

    assert not body.startswith(b"PK")
    response = await auth_client.post(
        "/settings/portability-v2/inspect",
        data={"passphrase": PASSPHRASE},
        files={"archive_file": ("record.vitals", body, "application/vnd.vitals.portability")},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    payload = response.json()
    assert payload["format"] == "vitals-portability-inspection"
    assert payload["version"] == 1
    assert uuid.UUID(payload["operation_id"]).int != 0
    assert payload["schema_digest"]
    assert payload["row_count"] >= 0
    assert payload["resource_count"] >= 0
    assert set(payload) == {
        "format",
        "version",
        "operation_id",
        "archive_id",
        "schema_digest",
        "row_count",
        "resource_count",
        "connections",
    }
    with open_validated_encrypted_archive(io.BytesIO(body), passphrase=PASSPHRASE) as archive:
        decoded = decode_validated_record(archive)
        assert decoded.row_count == payload["row_count"]


async def test_inspection_exposes_only_usable_same_subject_mapping_candidates(
    auth_client,
    db_session,
    legacy_owner_roots,
    garmin_connection_id,
):
    db_session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            integration_connection_id=garmin_connection_id,
            date=date(2026, 8, 25),
            weight_kg=80.0,
            domain="weight",
            source="manual",
        )
    )
    await db_session.commit()
    body = await _encrypted_export(auth_client)

    response = await auth_client.post(
        "/settings/portability-v2/inspect",
        data={"passphrase": PASSPHRASE},
        files={"archive_file": ("record.vitals", body, "application/octet-stream")},
    )

    assert response.status_code == 200
    descriptors = response.json()["connections"]
    garmin = next(item for item in descriptors if item["provider"] == "garmin")
    assert garmin["connection_type"] == "account"
    assert garmin["candidates"] == [
        {
            "id": str(garmin_connection_id),
            "label": f"garmin · account · {str(garmin_connection_id)[:8]}",
        }
    ]
    serialized = response.text
    assert "credential_ref" not in serialized
    assert "external_account_discriminator" not in serialized


async def test_wrong_passphrase_and_wrong_extension_fail_closed(auth_client):
    body = await _encrypted_export(auth_client)

    wrong_phrase = await auth_client.post(
        "/settings/portability-v2/inspect",
        data={"passphrase": "different protected record phrase"},
        files={"archive_file": ("record.vitals", body, "application/octet-stream")},
    )
    wrong_extension = await auth_client.post(
        "/settings/portability-v2/inspect",
        data={"passphrase": PASSPHRASE},
        files={"archive_file": ("record.json", body, "application/json")},
    )

    assert wrong_phrase.status_code == 400
    assert "passphrase" not in wrong_phrase.text.lower()
    assert wrong_extension.status_code == 415


async def test_apply_uses_authenticated_owner_and_explicit_inspected_mapping(
    auth_client,
    legacy_owner_roots,
    monkeypatch,
):
    from vitals.operations.portability import import_v2
    from web.routers import portability_v2

    body = await _encrypted_export(auth_client)
    inspected = await auth_client.post(
        "/settings/portability-v2/inspect",
        data={"passphrase": PASSPHRASE},
        files={"archive_file": ("record.vitals", body, "application/octet-stream")},
    )
    operation_id = inspected.json()["operation_id"]
    captured = {}

    async def import_stub(_factory, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            replayed=False,
            receipt=SimpleNamespace(request=SimpleNamespace(row_count=3, resource_count=1)),
            retirement_plan=None,
        )

    monkeypatch.setattr(portability_v2, "import_validated_record_v2", import_stub)
    response = await auth_client.post(
        "/settings/portability-v2/apply",
        data={
            "passphrase": PASSPHRASE,
            "operation_id": operation_id,
            "connection_mapping": "{}",
            "confirmation": "replace",
        },
        files={"archive_file": ("record.vitals", body, "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "status": "imported",
        "row_count": 3,
        "resource_count": 1,
        "cleanup_pending": False,
    }
    assert captured["target_subject_id"] == legacy_owner_roots.subject_id
    assert captured["actor_user_id"] == legacy_owner_roots.user_id
    assert captured["operation_id"] == uuid.UUID(operation_id)
    assert captured["connection_ids_by_ref"] == {}
    assert isinstance(captured["record"], import_v2.DecodedRecord)


async def test_apply_requires_literal_confirmation_before_import(
    auth_client,
    monkeypatch,
):
    from web.routers import portability_v2

    body = await _encrypted_export(auth_client)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unconfirmed apply reached the coordinator")

    monkeypatch.setattr(portability_v2, "import_validated_record_v2", forbidden)
    response = await auth_client.post(
        "/settings/portability-v2/apply",
        data={
            "passphrase": PASSPHRASE,
            "operation_id": str(uuid.uuid4()),
            "connection_mapping": "{}",
            "confirmation": "no",
        },
        files={"archive_file": ("record.vitals", body, "application/octet-stream")},
    )

    assert response.status_code == 400


async def test_all_v2_transfers_require_recent_authentication(
    client,
    legacy_owner_roots,
):
    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username="tester",
            user_id=legacy_owner_roots.user_id,
            session_version=1,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()) - 3600,
            subject_id=legacy_owner_roots.subject_id,
        ),
    )
    common = {
        "data": {"passphrase": PASSPHRASE},
        "headers": {"Referer": "http://test/settings", "Accept": "application/json"},
        "follow_redirects": False,
    }
    export = await client.post("/settings/portability-v2/export", **common)
    inspect = await client.post(
        "/settings/portability-v2/inspect",
        files={"archive_file": ("record.vitals", b"bad", "application/octet-stream")},
        **common,
    )
    apply = await client.post(
        "/settings/portability-v2/apply",
        data={
            "passphrase": PASSPHRASE,
            "operation_id": str(uuid.uuid4()),
            "connection_mapping": "{}",
            "confirmation": "replace",
        },
        files={"archive_file": ("record.vitals", b"bad", "application/octet-stream")},
        headers=common["headers"],
        follow_redirects=False,
    )

    assert export.status_code == inspect.status_code == apply.status_code == 401
