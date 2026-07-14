"""Add payment tables: transactions, stripe_account_id on advisor_profiles

Revision ID: b8d4f6c1e2a3
Revises: a7e2c5b8f1d9
Create Date: 2026-06-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "b8d4f6c1e2a3"
down_revision: str | None = "a7e2c5b8f1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRANSACTION_STATUS = PgEnum(
    "pending",
    "succeeded",
    "refunded",
    "failed",
    name="transaction_status",
    create_type=False,
)


def _create_enum_safe(name: str, values: str) -> None:
    """Create a PG enum type, silently skipping if it already exists."""
    op.execute(
        sa.text(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({values}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )


def upgrade() -> None:
    _create_enum_safe(
        "transaction_status",
        "'pending','succeeded','refunded','failed'",
    )

    op.add_column(
        "advisor_profiles",
        sa.Column("stripe_account_id", sa.String(100), nullable=True),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("stripe_checkout_session_id", sa.String(255), nullable=False),
        sa.Column("stripe_payment_intent_id", sa.String(255), nullable=True),
        sa.Column("stripe_charge_id", sa.String(255), nullable=True),
        sa.Column("amount_usd", sa.Float(), nullable=False),
        sa.Column("commission_rate", sa.Float(), nullable=False),
        sa.Column("commission_usd", sa.Float(), nullable=False),
        sa.Column("advisor_payout_usd", sa.Float(), nullable=False),
        sa.Column("status", _TRANSACTION_STATUS, nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_by", sa.Uuid(), nullable=True),
        sa.Column("refund_reason", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "is_archived",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id"),
        sa.UniqueConstraint("stripe_checkout_session_id"),
    )
    op.create_index("ix_transactions_booking_id", "transactions", ["booking_id"])
    op.create_index(
        "ix_transactions_stripe_checkout_session_id",
        "transactions",
        ["stripe_checkout_session_id"],
    )
    op.create_index(
        "ix_transactions_stripe_payment_intent_id",
        "transactions",
        ["stripe_payment_intent_id"],
    )
    op.create_index("ix_transactions_is_archived", "transactions", ["is_archived"])


def downgrade() -> None:
    op.drop_index("ix_transactions_is_archived", table_name="transactions")
    op.drop_index("ix_transactions_stripe_payment_intent_id", table_name="transactions")
    op.drop_index("ix_transactions_stripe_checkout_session_id", table_name="transactions")
    op.drop_index("ix_transactions_booking_id", table_name="transactions")
    op.drop_table("transactions")
    op.drop_column("advisor_profiles", "stripe_account_id")
    op.execute(sa.text("DROP TYPE IF EXISTS transaction_status"))
