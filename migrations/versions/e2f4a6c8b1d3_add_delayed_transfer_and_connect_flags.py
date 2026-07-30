"""add_delayed_transfer_and_connect_flags

Delayed Connect payout: hold the advisor's share for a window after payment, then
transfer it via a background sweep. Adds transfer bookkeeping to transactions, cached
Connect readiness flags to advisor profiles, and new transaction-event types.

Revision ID: e2f4a6c8b1d3
Revises: a1b3c5d7e9f0
Create Date: 2026-07-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f4a6c8b1d3"
down_revision: str | None = "a1b3c5d7e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_enum_safe(name: str, values: str) -> None:
    """Create a PG enum type, silently skipping if it already exists."""
    op.execute(
        sa.text(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({values}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )


def upgrade() -> None:
    # New transfer_status enum type (create_type=False on the column below).
    _create_enum_safe(
        "transfer_status",
        "'none','pending','completed','cancelled','failed'",
    )

    # New transaction_event_type values must be committed before use on Postgres.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE transaction_event_type ADD VALUE IF NOT EXISTS 'transfer_scheduled'"
        )
        op.execute(
            "ALTER TYPE transaction_event_type ADD VALUE IF NOT EXISTS 'transfer_completed'"
        )
        op.execute(
            "ALTER TYPE transaction_event_type ADD VALUE IF NOT EXISTS 'transfer_failed'"
        )

    transfer_status = sa.Enum(
        "none",
        "pending",
        "completed",
        "cancelled",
        "failed",
        name="transfer_status",
        create_type=False,
    )

    op.add_column(
        "transactions",
        sa.Column("transfer_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column(
            "transfer_status",
            transfer_status,
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "transactions",
        sa.Column("stripe_transfer_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column(
            "transfer_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "transactions",
        sa.Column("transfer_last_error", sa.String(500), nullable=True),
    )
    op.create_index(
        "ix_transactions_transfer_after", "transactions", ["transfer_after"]
    )

    # Cached Connect readiness flags on advisor profiles.
    for col in ("stripe_charges_enabled", "stripe_payouts_enabled", "stripe_details_submitted"):
        op.add_column(
            "advisor_profiles",
            sa.Column(col, sa.Boolean(), nullable=False, server_default="false"),
        )


def downgrade() -> None:
    for col in ("stripe_details_submitted", "stripe_payouts_enabled", "stripe_charges_enabled"):
        op.drop_column("advisor_profiles", col)

    op.drop_index("ix_transactions_transfer_after", table_name="transactions")
    op.drop_column("transactions", "transfer_last_error")
    op.drop_column("transactions", "transfer_attempts")
    op.drop_column("transactions", "stripe_transfer_id")
    op.drop_column("transactions", "transfer_status")
    op.drop_column("transactions", "transfer_after")

    op.execute(sa.text("DROP TYPE IF EXISTS transfer_status"))
    # Note: transaction_event_type ADD VALUEs are not removed — Postgres cannot drop
    # individual enum values, and leaving them is harmless.
