"""destination charges, refund ledger, booking refund state, new enum values

EPIC 04 Phase 1 (PAY-102 / PAY-104 / PAY-105) plus the notification and event
types the later phases use.

Revision ID: f6c8d0e2a4b6
Revises: e5b7c9d1f3a5
Create Date: 2026-09-24
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f6c8d0e2a4b6"
down_revision = "e5b7c9d1f3a5"
branch_labels: str | None = None
depends_on: str | None = None

_CHARGE_MODEL = postgresql.ENUM(
    "separate_transfer", "destination", name="charge_model", create_type=False
)
_REFUND_KIND = postgresql.ENUM(
    "advisor_cancel",
    "admin_full",
    "admin_partial",
    "expiry",
    "rejection",
    name="refund_kind",
    create_type=False,
)
_REFUND_STATUS = postgresql.ENUM(
    "pending", "refunded", "reversed", "failed", name="refund_status", create_type=False
)
_BOOKING_REFUND_STATUS = postgresql.ENUM(
    "none", "pending", "refunded", "failed", name="booking_refund_status", create_type=False
)

# notifications.type is a plain String column (no Postgres enum), so the new
# notification types need no ALTER TYPE; only these two real enums grow.
_NEW_ENTITY_TYPES = ("subscription",)
_NEW_EVENT_TYPES = ("refund_requested", "transfer_reversed", "refund_failed")


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (_CHARGE_MODEL, _REFUND_KIND, _REFUND_STATUS, _BOOKING_REFUND_STATUS):
        enum.create(bind, checkfirst=True)

    # Enum values must be committed before any row uses them.
    with op.get_context().autocommit_block():
        for value in _NEW_ENTITY_TYPES:
            op.execute(f"ALTER TYPE notification_entity_type ADD VALUE IF NOT EXISTS '{value}'")
        for value in _NEW_EVENT_TYPES:
            op.execute(f"ALTER TYPE transaction_event_type ADD VALUE IF NOT EXISTS '{value}'")

    # transactions: historical rows are separate-charge; new rows are destination.
    op.add_column(
        "transactions",
        sa.Column(
            "charge_model", _CHARGE_MODEL, nullable=False, server_default="separate_transfer"
        ),
    )
    op.add_column(
        "transactions",
        sa.Column("application_fee_usd", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "transactions", sa.Column("stripe_application_fee_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "transactions",
        sa.Column("advisor_reversed_usd", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "transactions",
        sa.Column(
            "platform_fee_refunded_usd", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
    )

    op.create_table(
        "transaction_refunds",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "transaction_id",
            sa.Uuid(),
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", _REFUND_KIND, nullable=False),
        sa.Column("refund_to_seeker_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("advisor_reversed_usd", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column(
            "platform_fee_refunded_usd", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        sa.Column("fee_policy_refunded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("stripe_refund_id", sa.String(255), nullable=True),
        sa.Column("stripe_reversal_id", sa.String(255), nullable=True),
        sa.Column("status", _REFUND_STATUS, nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("initiated_by", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_transaction_refunds_transaction_id", "transaction_refunds", ["transaction_id"]
    )
    op.create_index("ix_transaction_refunds_status", "transaction_refunds", ["status"])

    op.add_column(
        "bookings",
        sa.Column("refund_status", _BOOKING_REFUND_STATUS, nullable=False, server_default="none"),
    )
    op.add_column(
        "bookings",
        sa.Column("reschedule_count", sa.Integer(), nullable=False, server_default="0"),
    )

    op.add_column(
        "platform_payment_settings",
        sa.Column("subscription_grace_days", sa.Integer(), nullable=False, server_default="3"),
    )


def downgrade() -> None:
    op.drop_column("platform_payment_settings", "subscription_grace_days")
    op.drop_column("bookings", "reschedule_count")
    op.drop_column("bookings", "refund_status")
    op.drop_index("ix_transaction_refunds_status", table_name="transaction_refunds")
    op.drop_index("ix_transaction_refunds_transaction_id", table_name="transaction_refunds")
    op.drop_table("transaction_refunds")
    for col in (
        "platform_fee_refunded_usd",
        "advisor_reversed_usd",
        "stripe_application_fee_id",
        "application_fee_usd",
        "charge_model",
    ):
        op.drop_column("transactions", col)
    bind = op.get_bind()
    for enum in (_BOOKING_REFUND_STATUS, _REFUND_STATUS, _REFUND_KIND, _CHARGE_MODEL):
        enum.drop(bind, checkfirst=True)
    # Added enum values on notification_entity_type / transaction_event_type are kept:
    # Postgres cannot remove individual enum values.
