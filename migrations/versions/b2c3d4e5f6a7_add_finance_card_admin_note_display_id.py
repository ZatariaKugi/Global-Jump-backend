"""add_finance_card_admin_note_display_id

Admin Finance FE: add card_brand/card_last4 on transactions, admin_note,
phone on advisor_profiles, and a display_id computed helper.

Revision ID: b2c3d4e5f6a7
Revises: a7c9e1f3b2d4
Create Date: 2026-08-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a7c9e1f3b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Card brand + last-4 digits from Stripe charge.
    op.add_column("transactions", sa.Column("card_brand", sa.String(50), nullable=True))
    op.add_column("transactions", sa.Column("card_last4", sa.String(4), nullable=True))

    # Free-text admin note on the payment.
    op.add_column("transactions", sa.Column("admin_note", sa.String(2000), nullable=True))

    # Phone on advisor_profiles (seeker_profiles already has phone).
    op.add_column("advisor_profiles", sa.Column("phone", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("advisor_profiles", "phone")
    op.drop_column("transactions", "admin_note")
    op.drop_column("transactions", "card_last4")
    op.drop_column("transactions", "card_brand")
