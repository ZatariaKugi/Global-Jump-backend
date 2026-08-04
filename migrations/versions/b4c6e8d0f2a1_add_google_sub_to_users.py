"""add_google_sub_to_users

Persists Google's stable account identifier (the ``sub`` claim) on the users
table so a Google identity is tied to the Google account rather than a
re-assignable email address.

Revision ID: b4c6e8d0f2a1
Revises: a3d8f1c5b7e9
Create Date: 2026-08-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c6e8d0f2a1"
down_revision: str | None = "a3d8f1c5b7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("google_sub", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "google_sub")
