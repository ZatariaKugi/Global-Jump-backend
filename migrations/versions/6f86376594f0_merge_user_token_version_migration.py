"""merge user token version migration

Revision ID: 6f86376594f0
Revises: a4b6c8d0e2f4, d4e6f8a0b2c4
Create Date: 2026-08-13 19:26:28.410640

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6f86376594f0"
down_revision: str | None = ("a4b6c8d0e2f4", "d4e6f8a0b2c4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
