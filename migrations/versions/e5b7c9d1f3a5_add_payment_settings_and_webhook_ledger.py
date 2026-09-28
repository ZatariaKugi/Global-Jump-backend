"""add platform payment settings, setting audit, stripe webhook event ledger

EPIC 04 Phase 0 (PAY-106 / PAY-107 / PAY-109 / PAY-115).

Revision ID: e5b7c9d1f3a5
Revises: d4f6a8b0c2e4
Create Date: 2026-09-24
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5b7c9d1f3a5"
down_revision = "d4f6a8b0c2e4"
branch_labels: str | None = None
depends_on: str | None = None

_COMMISSION_TYPE = postgresql.ENUM("percent", "fixed", name="commission_type", create_type=False)
_FEE_REFUND = postgresql.ENUM("retained", "refunded", name="fee_refund_behavior", create_type=False)
_WEBHOOK_STATUS = postgresql.ENUM(
    "received", "processed", "failed", "ignored", name="webhook_event_status", create_type=False
)


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False, index=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    _COMMISSION_TYPE.create(bind, checkfirst=True)
    _FEE_REFUND.create(bind, checkfirst=True)
    _WEBHOOK_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "platform_payment_settings",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "seeker_reschedule_window_hours", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column(
            "advisor_cancellation_window_hours", sa.Integer(), nullable=False, server_default="2"
        ),
        sa.Column("commission_type", _COMMISSION_TYPE, nullable=False, server_default="percent"),
        sa.Column("commission_value", sa.Numeric(10, 2), nullable=False, server_default="15"),
        sa.Column(
            "platform_fee_refund_behavior", _FEE_REFUND, nullable=False, server_default="retained"
        ),
        sa.Column("live_payments_enabled", sa.Boolean(), nullable=False, server_default="false"),
        *_audit_columns(),
    )

    op.create_table(
        "platform_setting_changes",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "settings_id",
            sa.Uuid(),
            sa.ForeignKey("platform_payment_settings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("setting_key", sa.String(64), nullable=False),
        sa.Column("old_value", sa.String(64), nullable=True),
        sa.Column("new_value", sa.String(64), nullable=True),
        sa.Column(
            "changed_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_platform_setting_changes_settings_id", "platform_setting_changes", ["settings_id"]
    )
    op.create_index(
        "ix_platform_setting_changes_setting_key", "platform_setting_changes", ["setting_key"]
    )
    op.create_index(
        "ix_platform_setting_changes_changed_by", "platform_setting_changes", ["changed_by"]
    )

    op.create_table(
        "stripe_webhook_events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("status", _WEBHOOK_STATUS, nullable=False, server_default="received"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_stripe_webhook_events_event_type", "stripe_webhook_events", ["event_type"])
    op.create_index("ix_stripe_webhook_events_status", "stripe_webhook_events", ["status"])


def downgrade() -> None:
    op.drop_index("ix_stripe_webhook_events_status", table_name="stripe_webhook_events")
    op.drop_index("ix_stripe_webhook_events_event_type", table_name="stripe_webhook_events")
    op.drop_table("stripe_webhook_events")
    op.drop_index("ix_platform_setting_changes_changed_by", table_name="platform_setting_changes")
    op.drop_index("ix_platform_setting_changes_setting_key", table_name="platform_setting_changes")
    op.drop_index("ix_platform_setting_changes_settings_id", table_name="platform_setting_changes")
    op.drop_table("platform_setting_changes")
    op.drop_table("platform_payment_settings")
    bind = op.get_bind()
    _WEBHOOK_STATUS.drop(bind, checkfirst=True)
    _FEE_REFUND.drop(bind, checkfirst=True)
    _COMMISSION_TYPE.drop(bind, checkfirst=True)
