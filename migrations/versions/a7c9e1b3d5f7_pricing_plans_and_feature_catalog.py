"""pricing plans, plan features, seeded feature catalog

EPIC 04 Phase 2 (PAY-112 / PAY-113 / PAY-114).

Revision ID: a7c9e1b3d5f7
Revises: f6c8d0e2a4b6
Create Date: 2026-09-24
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a7c9e1b3d5f7"
down_revision = "f6c8d0e2a4b6"
branch_labels: str | None = None
depends_on: str | None = None

_AUDIENCE = postgresql.ENUM("seeker", "advisor", name="plan_audience", create_type=False)
_STATUS = postgresql.ENUM("draft", "active", "inactive", name="plan_status", create_type=False)
_INTERVAL = postgresql.ENUM("month", name="billing_interval", create_type=False)
_KIND = postgresql.ENUM("enforceable", "display", name="feature_kind", create_type=False)
_VALUE_TYPE = postgresql.ENUM(
    "number", "bool", "text", name="feature_value_type", create_type=False
)

CATALOG = (
    ("ai_assessments", "AI assessments per month", "ai_assessment", "seeker", "number", 10),
    ("ai_insights", "AI insights on results", "ai_assessment", "seeker", "bool", 20),
    ("assessment_history", "Assessment history", "ai_assessment", "seeker", "number", 30),
    ("matched_advisors", "Recommended advisors", "matching", "seeker", "number", 40),
    ("find_advisor", "Find Advisor search", "advisors", "seeker", "bool", 50),
    ("bookmarks", "Bookmarked advisors", "bookmarks", "seeker", "number", 60),
    ("consultations", "Consultations per month", "bookings", "seeker", "number", 70),
    ("chats", "Chat with advisors", "conversations", "seeker", "bool", 80),
    ("documents", "Document uploads", "documents", "seeker", "number", 90),
    ("visa_journey", "Visa Journey", "visa_journey", "seeker", "bool", 100),
    ("priority_support", "Priority support", "support", "both", "bool", 110),
    ("leads", "Leads per month", "leads", "advisor", "number", 120),
    ("featured_listing", "Featured listing", "advisors", "advisor", "bool", 130),
    ("client_bookings", "Client bookings per month", "bookings", "advisor", "number", 140),
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
    for enum in (_AUDIENCE, _STATUS, _INTERVAL, _KIND, _VALUE_TYPE):
        enum.create(bind, checkfirst=True)

    catalog = op.create_table(
        "plan_feature_catalog",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("limit_type", sa.String(16), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.bulk_insert(
        catalog,
        [
            {
                "key": key,
                "label": label,
                "module": module,
                "audience": audience,
                "limit_type": limit_type,
                "display_order": order,
            }
            for key, label, module, audience, limit_type, order in CATALOG
        ],
    )

    op.create_table(
        "pricing_plans",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column("audience", _AUDIENCE, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("tagline", sa.String(200), nullable=True),
        sa.Column("price_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="usd"),
        sa.Column("billing_interval", _INTERVAL, nullable=False, server_default="month"),
        sa.Column("status", _STATUS, nullable=False, server_default="draft"),
        sa.Column("is_highlighted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("stripe_product_id", sa.String(255), nullable=True),
        sa.Column("stripe_price_id", sa.String(255), nullable=True),
        sa.Column("price_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sync_error", sa.String(500), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
    )
    op.create_index("ix_pricing_plans_audience", "pricing_plans", ["audience"])
    op.create_index("ix_pricing_plans_status", "pricing_plans", ["status"])

    op.create_table(
        "pricing_plan_features",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "plan_id",
            sa.Uuid(),
            sa.ForeignKey("pricing_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("feature_key", sa.String(64), nullable=True),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("value_type", _VALUE_TYPE, nullable=False),
        sa.Column("value", sa.String(64), nullable=False, server_default=""),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_pricing_plan_features_plan_id", "pricing_plan_features", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_pricing_plan_features_plan_id", table_name="pricing_plan_features")
    op.drop_table("pricing_plan_features")
    op.drop_index("ix_pricing_plans_status", table_name="pricing_plans")
    op.drop_index("ix_pricing_plans_audience", table_name="pricing_plans")
    op.drop_table("pricing_plans")
    op.drop_table("plan_feature_catalog")
    bind = op.get_bind()
    for enum in (_VALUE_TYPE, _KIND, _INTERVAL, _STATUS, _AUDIENCE):
        enum.drop(bind, checkfirst=True)
