"""Stripe Tax at checkout, and the Stripe processing fee on each payment.

QA document "Global Jump — Tax Execution, Refunds & Stripe Dashboard Implementation"
(2026-10-07/08). Two things the books could not say until now:

1. **Tax.** A seeker pays consultation + tax, and **Stripe Tax** decides the tax from
   the seeker's billing address when the admin switch
   ``platform_payment_settings.automatic_tax_enabled`` is on. The transaction already
   had ``tax_rate`` / ``tax_usd`` (always 0 since decision D4); ``tax_label``,
   ``tax_country`` and ``tax_jurisdiction`` snapshot what Stripe charged.

2. **Stripe's processing fee.** On a destination charge the platform's balance pays
   it. The business rule is that the advisor bears it, so at payment time the backend
   reads the real fee off the charge's balance transaction (``stripe_fee_usd``,
   ``stripe_balance_transaction_id``) and reverses exactly that amount from the
   advisor's transfer (``stripe_fee_reversal_id``; NULL means the recovery failed and
   the advisor still holds it). Historical rows read 0: the fee was paid, but never
   recorded, so there is nothing to backfill without re-reading every charge.

Also adds ``stripe_fee_recovered`` to the transaction event enum.

Revision ID: b7e3c9a1d5f2
Revises: a3d7f1b9c5e2
Create Date: 2026-10-08 11:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3c9a1d5f2"
down_revision: str | None = "a3d7f1b9c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_payment_settings",
        sa.Column("automatic_tax_enabled", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "transactions", sa.Column("tax_jurisdiction", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "transactions",
        sa.Column(
            "stripe_fee_usd", sa.Numeric(precision=10, scale=2), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "transactions", sa.Column("stripe_fee_reversal_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "transactions",
        sa.Column("stripe_balance_transaction_id", sa.String(length=255), nullable=True),
    )
    op.add_column("transactions", sa.Column("tax_label", sa.String(length=50), nullable=True))
    op.add_column("transactions", sa.Column("tax_country", sa.String(length=2), nullable=True))

    # Postgres enum values are appended in place; ADD VALUE cannot run inside the
    # migration transaction on older servers, hence the autocommit block.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE transaction_event_type ADD VALUE IF NOT EXISTS 'stripe_fee_recovered'"
        )


def downgrade() -> None:
    # The enum value stays: Postgres cannot drop one, and rows may reference it.
    op.drop_column("transactions", "tax_country")
    op.drop_column("transactions", "tax_label")
    op.drop_column("transactions", "stripe_balance_transaction_id")
    op.drop_column("transactions", "stripe_fee_reversal_id")
    op.drop_column("transactions", "stripe_fee_usd")
    op.drop_column("transactions", "tax_jurisdiction")
    op.drop_column("platform_payment_settings", "automatic_tax_enabled")
