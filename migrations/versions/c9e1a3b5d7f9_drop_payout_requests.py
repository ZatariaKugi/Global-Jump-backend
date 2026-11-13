"""drop payout_requests (manual payout module retired, EPIC 04 decision D3)

Destination charges pay the advisor inside the charge, so the "available balance"
ledger and payout requests no longer describe real money. Any pending rows referred
to legacy separate-charge payments and must be settled by hand before this runs.

Revision ID: c9e1a3b5d7f9
Revises: b8d0f2c4e6a8
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c9e1a3b5d7f9"
down_revision = "b8d0f2c4e6a8"
branch_labels: str | None = None
depends_on: str | None = None

_METHOD = postgresql.ENUM(
    "bank_transfer", "paypal", "stripe", name="payout_method", create_type=False
)
_STATUS = postgresql.ENUM(
    "pending", "completed", "rejected", name="payout_status", create_type=False
)


def upgrade() -> None:
    op.drop_index("ix_payout_requests_is_archived", table_name="payout_requests")
    op.drop_index("ix_payout_requests_advisor_id", table_name="payout_requests")
    op.drop_table("payout_requests")
    bind = op.get_bind()
    _STATUS.drop(bind, checkfirst=True)
    _METHOD.drop(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    _METHOD.create(bind, checkfirst=True)
    _STATUS.create(bind, checkfirst=True)
    op.create_table(
        "payout_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "advisor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("amount_usd", sa.Float(), nullable=False),
        sa.Column("method", _METHOD, nullable=False),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("account_holder_name", sa.String(255), nullable=True),
        sa.Column("account_number", sa.String(64), nullable=True),
        sa.Column("bank_name", sa.String(255), nullable=True),
        sa.Column("swift_code", sa.String(32), nullable=True),
        sa.Column("processing_fee_usd", sa.Float(), nullable=False),
        sa.Column("net_amount_usd", sa.Float(), nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_by", sa.Uuid(), nullable=True),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index("ix_payout_requests_advisor_id", "payout_requests", ["advisor_id"])
    op.create_index("ix_payout_requests_is_archived", "payout_requests", ["is_archived"])
