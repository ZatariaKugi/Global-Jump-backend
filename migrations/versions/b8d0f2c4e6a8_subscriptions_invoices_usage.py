"""subscriptions, subscription invoices, metered usage, customer id, advisor cache

EPIC 04 Phase 3 (PAY-110 / PAY-111).

Revision ID: b8d0f2c4e6a8
Revises: a7c9e1b3d5f7
Create Date: 2026-09-24
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8d0f2c4e6a8"
down_revision = "a7c9e1b3d5f7"
branch_labels: str | None = None
depends_on: str | None = None

_SUB_STATUS = postgresql.ENUM(
    "incomplete",
    "trialing",
    "active",
    "past_due",
    "canceled",
    "unpaid",
    "expired",
    name="subscription_status",
    create_type=False,
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
    _SUB_STATUS.create(op.get_bind(), checkfirst=True)

    op.add_column("users", sa.Column("stripe_customer_id", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_users_stripe_customer_id", "users", ["stripe_customer_id"])
    op.add_column(
        "advisor_profiles",
        sa.Column("subscription_status", sa.String(20), nullable=False, server_default="none"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "plan_id",
            sa.Uuid(),
            sa.ForeignKey("pricing_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stripe_subscription_id", sa.String(255), nullable=True, unique=True),
        sa.Column("stripe_price_id", sa.String(255), nullable=True),
        sa.Column("status", _SUB_STATUS, nullable=False, server_default="incomplete"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_method_brand", sa.String(32), nullable=True),
        sa.Column("payment_method_last4", sa.String(4), nullable=True),
        sa.Column("payment_method_exp_month", sa.Integer(), nullable=True),
        sa.Column("payment_method_exp_year", sa.Integer(), nullable=True),
        *_audit_columns(),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    op.create_table(
        "subscription_invoices",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "subscription_id",
            sa.Uuid(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stripe_invoice_id", sa.String(255), nullable=False, unique=True),
        sa.Column("stripe_invoice_number", sa.String(64), nullable=True),
        sa.Column("description", sa.String(200), nullable=True),
        sa.Column("amount_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hosted_invoice_url", sa.String(500), nullable=True),
        sa.Column("invoice_pdf_url", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_subscription_invoices_subscription_id", "subscription_invoices", ["subscription_id"]
    )

    op.create_table(
        "subscription_usage",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("feature_key", sa.String(64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "user_id", "feature_key", "period_start", name="uq_usage_user_key_period"
        ),
    )
    op.create_index("ix_subscription_usage_user_id", "subscription_usage", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_subscription_usage_user_id", table_name="subscription_usage")
    op.drop_table("subscription_usage")
    op.drop_index("ix_subscription_invoices_subscription_id", table_name="subscription_invoices")
    op.drop_table("subscription_invoices")
    op.drop_index("ix_subscriptions_status", table_name="subscriptions")
    op.drop_index("ix_subscriptions_plan_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_column("advisor_profiles", "subscription_status")
    op.drop_constraint("uq_users_stripe_customer_id", "users", type_="unique")
    op.drop_column("users", "stripe_customer_id")
    _SUB_STATUS.drop(op.get_bind(), checkfirst=True)
