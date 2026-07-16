"""add_user_is_suspended

Revision ID: c3d5e7f9a2b4
Revises: b2c4e6f8a1d3
Create Date: 2026-07-16 02:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d5e7f9a2b4"
down_revision: str | None = "b2c4e6f8a1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_suspended",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_suspended")
