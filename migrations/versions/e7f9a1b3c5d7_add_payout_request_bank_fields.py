"""add_payout_request_bank_fields

Revision ID: e7f9a1b3c5d7
Revises: d6e8f0a2b4c6
Create Date: 2026-07-16 06:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f9a1b3c5d7"
down_revision: str | None = "d6e8f0a2b4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payout_requests",
        sa.Column("account_holder_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "payout_requests",
        sa.Column("account_number", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "payout_requests",
        sa.Column("bank_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "payout_requests",
        sa.Column("swift_code", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payout_requests", "swift_code")
    op.drop_column("payout_requests", "bank_name")
    op.drop_column("payout_requests", "account_number")
    op.drop_column("payout_requests", "account_holder_name")
