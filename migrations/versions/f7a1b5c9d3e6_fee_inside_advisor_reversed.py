"""fee_inside_advisor_reversed

``advisor_reversed_usd`` is everything reversed off the advisor's transfer, Stripe's fee
included (PM exercise, 2026-10-10). Rows where the fee was taken but never reconciled
into that figure get it added once; rows already reconciled by ``transfer.reversed``
(reversed >= fee) are left alone.

Revision ID: f7a1b5c9d3e6
Revises: e6f0a4b8c2d5
Create Date: 2026-10-10 12:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a1b5c9d3e6"
down_revision: str | None = "e6f0a4b8c2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE transactions SET advisor_reversed_usd = advisor_reversed_usd + stripe_fee_usd "
        "WHERE stripe_fee_reversal_id IS NOT NULL AND stripe_fee_usd > 0 "
        "AND advisor_reversed_usd < stripe_fee_usd"
    )


def downgrade() -> None:
    pass  # a data correction; nothing to undo
