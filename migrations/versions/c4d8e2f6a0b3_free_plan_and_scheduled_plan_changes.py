"""Free plan foundation, scheduled plan changes, and the perk catalog rework.

PM decisions of 2026-10-08 (QA doc "BUGs Global Jump STRIPE CONFIGURATIONS" BUG11-23,
and the free-plan discussion document):

1. ``pricing_plans.is_default`` — the audience's designated free plan. Until now the
   oldest active $0 plan applied by accident (BUG12). Backfilled from exactly that
   rule so nothing changes for a configured environment.
2. ``users.plan_choice_acknowledged_at`` — the first-time plans page is enforced
   server-side; anyone who already holds a subscription row is marked acknowledged.
3. ``subscriptions.scheduled_plan_id`` / ``scheduled_change_at`` / ``stripe_schedule_id``
   — a downgrade or cancel waits for the period end (BUG17).
4. ``subscriptions.plan_version`` + ``subscription_features`` — the subscriber's perks
   are a snapshot, so an admin edit never reaches a live subscriber (BUG21). Existing
   subscriptions are backfilled from their plan's current rows.
5. Catalog: ``consultations``, ``client_bookings``, ``find_advisor`` and
   ``featured_listing`` are retired (bookings are core, advisor discovery is free,
   featured listing is removed for now; ``visa_journey`` stays — the PM kept it as a
   seeker module lock on 2026-10-09); ``priority_support``
   becomes ``support`` (the whole module is one paid perk); ``chats`` applies to both
   audiences; ``profile_edits`` and ``ai_recommended`` are new advisor perks.

Revision ID: c4d8e2f6a0b3
Revises: b7e3c9a1d5f2
Create Date: 2026-10-08 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d8e2f6a0b3"
down_revision: str | None = "b7e3c9a1d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETIRED_KEYS = (
    "consultations",
    "client_bookings",
    "find_advisor",
    "featured_listing",
    "ai_insights",
)
NEW_KEYS = (
    ("profile_edits", "Profile edits", "advisors", "advisor", "number", 125),
    ("ai_recommended", "Appears in AI recommendations", "matching", "advisor", "bool", 135),
)


def upgrade() -> None:
    op.add_column(
        "pricing_plans",
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "users", sa.Column("plan_choice_acknowledged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "scheduled_plan_id",
            sa.Uuid(),
            sa.ForeignKey("pricing_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("scheduled_change_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions", sa.Column("stripe_schedule_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "subscriptions",
        sa.Column("plan_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "subscriptions", sa.Column("price_usd", sa.Numeric(precision=10, scale=2), nullable=True)
    )
    op.create_table(
        "subscription_features",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=64), server_default="", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_subscription_features_subscription_id",
        "subscription_features",
        ["subscription_id"],
    )

    # 1. the default free plan: the oldest active $0 plan per audience, as before
    op.execute(
        """
        UPDATE pricing_plans SET is_default = true
        WHERE id IN (
            SELECT DISTINCT ON (audience) id FROM pricing_plans
            WHERE status = 'active' AND price_usd <= 0
            ORDER BY audience, created_at
        )
        """
    )
    # 2. anyone already subscribed has chosen a plan
    op.execute(
        """
        UPDATE users SET plan_choice_acknowledged_at = NOW()
        WHERE id IN (SELECT DISTINCT user_id FROM subscriptions)
        """
    )
    # 4. snapshot every existing subscription from its plan's current rows
    op.execute(
        """
        INSERT INTO subscription_features (id, subscription_id, feature_key, label, value_type, value, is_enabled)
        SELECT gen_random_uuid(), s.id, f.feature_key, f.label, f.value_type::text, f.value, f.is_enabled
        FROM subscriptions s
        JOIN pricing_plan_features f ON f.plan_id = s.plan_id
        WHERE f.kind = 'enforceable' AND f.feature_key IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE subscriptions s SET plan_version = GREATEST(p.price_version, 1), price_usd = p.price_usd
        FROM pricing_plans p WHERE p.id = s.plan_id
        """
    )
    # 5. catalog rework
    retired = ", ".join(f"'{k}'" for k in RETIRED_KEYS)
    op.execute(f"DELETE FROM pricing_plan_features WHERE feature_key IN ({retired})")
    op.execute(f"DELETE FROM subscription_features WHERE feature_key IN ({retired})")
    op.execute(f"DELETE FROM plan_feature_catalog WHERE key IN ({retired})")
    op.execute(
        "UPDATE plan_feature_catalog SET key = 'support', label = 'Support' "
        "WHERE key = 'priority_support'"
    )
    op.execute(
        "UPDATE pricing_plan_features SET feature_key = 'support', label = 'Support' "
        "WHERE feature_key = 'priority_support'"
    )
    op.execute(
        "UPDATE subscription_features SET feature_key = 'support', label = 'Support' "
        "WHERE feature_key = 'priority_support'"
    )
    op.execute("UPDATE plan_feature_catalog SET audience = 'both', label = 'Chat' WHERE key = 'chats'")
    for key, label, module, audience, limit_type, order in NEW_KEYS:
        op.execute(
            "INSERT INTO plan_feature_catalog (key, label, module, audience, limit_type, display_order) "
            f"VALUES ('{key}', '{label}', '{module}', '{audience}', '{limit_type}', {order}) "
            "ON CONFLICT (key) DO NOTHING"
        )


def downgrade() -> None:
    op.execute("DELETE FROM plan_feature_catalog WHERE key IN ('profile_edits', 'ai_recommended')")
    op.execute(
        "UPDATE plan_feature_catalog SET key = 'priority_support', label = 'Priority support' "
        "WHERE key = 'support'"
    )
    op.execute(
        "UPDATE pricing_plan_features SET feature_key = 'priority_support' "
        "WHERE feature_key = 'support'"
    )
    op.execute("UPDATE plan_feature_catalog SET audience = 'seeker' WHERE key = 'chats'")
    op.drop_index("ix_subscription_features_subscription_id", table_name="subscription_features")
    op.drop_table("subscription_features")
    op.drop_column("subscriptions", "price_usd")
    op.drop_column("subscriptions", "plan_version")
    op.drop_column("subscriptions", "stripe_schedule_id")
    op.drop_column("subscriptions", "scheduled_change_at")
    op.drop_column("subscriptions", "scheduled_plan_id")
    op.drop_column("users", "plan_choice_acknowledged_at")
    op.drop_column("pricing_plans", "is_default")
