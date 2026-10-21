"""account_execution_locks_m3_3

Revision ID: d3f820c4a11b
Revises: c2e171b3f94a
Create Date: 2026-10-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f820c4a11b"
down_revision: str | Sequence[str] | None = "c2e171b3f94a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema for Milestone M3.3: add account_execution_locks table."""
    op.create_table(
        "account_execution_locks",
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
        sa.UniqueConstraint("job_id", name="uq_account_execution_locks_job_id"),
    )
    with op.batch_alter_table("account_execution_locks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_account_execution_locks_job_id"),
            ["job_id"],
            unique=True,
        )


def downgrade() -> None:
    """Downgrade schema for Milestone M3.3."""
    with op.batch_alter_table("account_execution_locks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_execution_locks_job_id"))
    op.drop_table("account_execution_locks")
