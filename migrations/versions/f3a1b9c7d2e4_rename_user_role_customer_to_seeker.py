"""rename user_role enum value customer to seeker

Revision ID: f3a1b9c7d2e4
Revises: 0958382a49ec
Create Date: 2026-06-24 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a1b9c7d2e4"
down_revision: str | None = "0958382a49ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE user_role RENAME VALUE 'customer' TO 'seeker'")
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'seeker'")


def downgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'customer'")
    op.execute("ALTER TYPE user_role RENAME VALUE 'seeker' TO 'customer'")
