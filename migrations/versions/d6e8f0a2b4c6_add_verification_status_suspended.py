"""add_verification_status_suspended_and_pre_suspend

Revision ID: d6e8f0a2b4c6
Revises: c3d5e7f9a2b4
Create Date: 2026-07-16 03:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e8f0a2b4c6"
down_revision: str | None = "c3d5e7f9a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New enum values must be committed before use on Postgres.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE verification_status ADD VALUE IF NOT EXISTS 'suspended'")

    op.add_column(
        "users",
        sa.Column(
            "pre_suspend_verification_status",
            sa.Enum(
                "pending",
                "under_review",
                "approved",
                "rejected",
                "suspended",
                name="verification_status",
                create_type=False,
            ),
            nullable=True,
        ),
    )
    # Backfill: soft-suspended advisors already in DB → expose as suspended in UI.
    op.execute(
        """
        UPDATE users
        SET pre_suspend_verification_status = verification_status,
            verification_status = 'suspended'
        WHERE is_suspended = true
          AND role = 'advisor'
          AND (verification_status IS NULL OR verification_status::text <> 'suspended')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE users
        SET verification_status = COALESCE(pre_suspend_verification_status, 'pending')
        WHERE verification_status = 'suspended'
        """
    )
    op.drop_column("users", "pre_suspend_verification_status")
    # Cannot remove enum value from PostgreSQL safely.
