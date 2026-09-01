"""Allow null profile_id on advisor_offered_services for global admin catalog rows."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e9f1a3b5c7d9"
down_revision = "d8e0f2a4b6c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "advisor_offered_services",
        "profile_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM advisor_offered_services WHERE profile_id IS NULL"
    )
    op.alter_column(
        "advisor_offered_services",
        "profile_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
