"""Give each care relationship one explicit stable conversation.

Historical topic threads cannot be identified safely from their title, age or
participant count: every one of those shapes can also describe the room a
patient still uses today.  The nullable relationship link therefore marks only
new canonical rooms. Existing rows remain NULL and keep every message,
participant and provenance field unchanged as readable history.

The unique constraint is the final concurrency boundary. The service locks the
relationship while opening its room, and the database still refuses a second
canonical row if another writer reaches the insert first.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0084"
down_revision: Union[str, None] = "0083"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("care_threads") as batch_op:
        batch_op.add_column(
            sa.Column(
                "canonical_relationship_id",
                sa.Uuid(as_uuid=True),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_care_threads_canonical_relationship",
            "care_relationships",
            ["canonical_relationship_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_care_threads_canonical_relationship",
            ["canonical_relationship_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("care_threads") as batch_op:
        batch_op.drop_constraint(
            "uq_care_threads_canonical_relationship",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_care_threads_canonical_relationship",
            type_="foreignkey",
        )
        batch_op.drop_column("canonical_relationship_id")
