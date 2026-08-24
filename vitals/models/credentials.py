"""The one table that holds a provider secret, and why it is not in tenancy.py.

``vitals/models/tenancy.py`` says in its own first paragraph that its models
establish durable ownership only and deliberately hold no provider secrets. That
is still true, and this file exists so it can stay true: ``credential_ref`` on
``IntegrationConnection`` is a resolver handle, and this is one of the things it
can resolve to.

**Why a secret is in the database at all.** Because it stopped being one secret.
Garmin and Hevy were configured in ``.env`` — one watch and one workout account
for the whole process — and that is a single-user shape a shared installation
cannot have. Running the four provider-sync jobs once per subject with those
credentials would write the operator's own watch data into everybody else's
record: an outage turned into a disclosure. A credential that belongs to a
person has to be stored per person, and the only per-person store here is the
database.

**What is actually stored.** A Fernet ciphertext of a small JSON object, and
nothing else that could identify the account. No email in a column, no key
suffix, no plaintext anywhere — ``external_account_discriminator`` on the
connection above stays opaque for exactly that reason. The encryption key is
``VITALS_CREDENTIAL_KEY`` and belongs to the installation, which is the kind of
thing ``.env`` is *for*; losing it costs every stored credential and no health
data, and re-entering them is a form somebody fills in.

``key_version`` is here so rotation is a migration rather than an outage: a
second key can decrypt the old rows while new ones are written under it. Nothing
rotates yet, and the column is the difference between that being a change and
being a rewrite.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from vitals.models.base import Base


class IntegrationCredential(Base):
    """The secret one subject's provider connection signs in with.

    Keyed by the connection rather than by the subject and provider together:
    the connection already carries that pair under a unique constraint, and a
    second row for the same connection is not a state this has a meaning for.
    """

    __tablename__ = "integration_credentials"
    __table_args__ = (
        PrimaryKeyConstraint(
            "integration_connection_id", name="pk_integration_credentials"
        ),
        # The composite parent key, not just the id. ``integration_connections``
        # carries ``uq_integration_connections_id_subject`` for this: it makes
        # "this credential's subject is its connection's subject" a thing the
        # database enforces rather than a thing every reader has to remember to
        # check. A row whose two owners disagree cannot exist.
        ForeignKeyConstraint(
            ["integration_connection_id", "subject_id"],
            [
                "integration_connections.id",
                "integration_connections.subject_id",
            ],
            name="fk_integration_credentials_connection_subject",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "key_version >= 1", name="ck_integration_credentials_key_version"
        ),
        CheckConstraint(
            "length(ciphertext) > 0", name="ck_integration_credentials_ciphertext"
        ),
    )

    integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    #: Duplicated from the connection so row security has a column to police.
    #: Every other subject-bearing table is reachable by the same policy text,
    #: and a table that needed its own would be a table somebody could forget.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["IntegrationCredential"]
