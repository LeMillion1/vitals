"""Bind a local user to the identity a provider authenticates, and stop holding passwords.

Revision ID: 0052
Revises: 0051
Create Date: 2026-08-23

Vitals is about to stop authenticating anybody itself.  An identity provider
does that, and what arrives back is a pair the provider guarantees: an opaque
``sub`` inside an ``iss`` namespace, immutable for the life of the account.
``user_federated_identities`` stores that pair and nothing else that identifies.

Email and display name arrive in the same token and are deliberately not stored
here as lookup keys.  A provider may let a person change either, and matching on
them would hand one account to whoever claimed the address next — which is the
classic account-takeover in federated login, not a hypothetical.

``users.password_hash`` becomes nullable.  Password material is the provider's
to hold: hashing, reset, breach response and rotation are all things it already
does properly, and a second copy here would be a second thing to get right.  The
column stays for the pre-cutover owner's migrated bcrypt hash, which is why this
loosens the constraint rather than dropping the column.

Downgrade drops the table and restores ``NOT NULL``.  That last part can only
succeed while every user still has a hash — after the cutover has provisioned
anybody through the provider, it cannot, and the failure is the honest one.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_federated_identities",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "issuer", "subject", name="uq_user_federated_identities_issuer_subject"
        ),
        sa.CheckConstraint(
            "length(trim(issuer)) > 0",
            name="ck_user_federated_identities_issuer_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(subject)) > 0",
            name="ck_user_federated_identities_subject_not_blank",
        ),
    )
    op.create_index(
        "ix_user_federated_identities_user_id",
        "user_federated_identities",
        ["user_id"],
    )

    # The blank check has to go before the column can be null, or an existing
    # row with no hash would satisfy neither.
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("ck_users_password_hash_not_blank", type_="check")
        batch.alter_column("password_hash", existing_type=sa.Text(), nullable=True)
        batch.create_check_constraint(
            "ck_users_password_hash_not_blank",
            "password_hash IS NULL OR length(trim(password_hash)) > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("ck_users_password_hash_not_blank", type_="check")
        batch.alter_column("password_hash", existing_type=sa.Text(), nullable=False)
        batch.create_check_constraint(
            "ck_users_password_hash_not_blank",
            "length(trim(password_hash)) > 0",
        )
    op.drop_index(
        "ix_user_federated_identities_user_id",
        table_name="user_federated_identities",
    )
    op.drop_table("user_federated_identities")
