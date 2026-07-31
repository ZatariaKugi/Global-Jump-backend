"""add_google_oauth_fields

Adds Google OAuth support to the users table:
  * ``auth_provider`` enum (local/google) — how the account was created/linked.
  * ``hashed_password`` becomes nullable — Google-only accounts have no password.

Revision ID: f7a9c2b4d6e8
Revises: e2f4a6c8b1d3
Create Date: 2026-07-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a9c2b4d6e8"
down_revision: str | None = "e2f4a6c8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create the enum type explicitly before adding the column that uses it.
    auth_provider_enum = sa.Enum("local", "google", name="auth_provider")
    auth_provider_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            auth_provider_enum,
            server_default="local",
            nullable=False,
        ),
    )
    # Existing rows already default to 'local' via server_default; be explicit.
    op.execute("UPDATE users SET auth_provider = 'local' WHERE auth_provider IS NULL")

    # Google-only accounts have no password.
    op.alter_column("users", "hashed_password", existing_type=sa.String(length=255), nullable=True)


def downgrade() -> None:
    # Backfill any NULL passwords before restoring NOT NULL would fail; guard by
    # leaving Google rows out is not possible, so this is a lossy downgrade —
    # only run it when no Google-only accounts exist.
    op.alter_column("users", "hashed_password", existing_type=sa.String(length=255), nullable=False)
    op.drop_column("users", "auth_provider")
    sa.Enum(name="auth_provider").drop(op.get_bind(), checkfirst=True)
