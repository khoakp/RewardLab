"""session_store_m3_2

Revision ID: c2e171b3f94a
Revises: 5577dab1c31c
Create Date: 2026-10-20 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2e171b3f94a"
down_revision: str | Sequence[str] | None = "5577dab1c31c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema for Milestone M3.2."""
    with op.batch_alter_table("session_refs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("encrypted_payload", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=32),
                server_default="ACTIVE",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_session_refs_status"), ["status"], unique=False)


def downgrade() -> None:
    """Downgrade schema for Milestone M3.2."""
    with op.batch_alter_table("session_refs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_session_refs_status"))
        batch_op.drop_column("revoked_at")
        batch_op.drop_column("status")
        batch_op.drop_column("encrypted_payload")
