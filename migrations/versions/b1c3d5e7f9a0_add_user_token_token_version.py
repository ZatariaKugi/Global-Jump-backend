"""add token_version to user_tokens

Revision ID: b1c3d5e7f9a0
Revises: 6f86376594f0
Create Date: 2026-08-14 00:25:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c3d5e7f9a0"
down_revision: str | None = "6f86376594f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_tokens",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_tokens", "token_version")
