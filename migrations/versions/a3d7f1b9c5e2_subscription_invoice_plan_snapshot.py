"""Record which plan each subscription invoice was charged for.

The admin Subscriptions Finance screen breaks revenue down by plan, and until now
there was nothing to break it down *by*: an invoice knew only its subscription, and
a subscription carries the plan the subscriber is on **right now**. So every invoice
a subscriber had ever paid was attributed to their current plan -- six months of
Plan A payments would appear under Plan C the moment they upgraded, making Plan A
look worthless and Plan C look like a star.

This is the same rule bookings already follow: snapshot the identity at the time of
the charge instead of resolving it later (``05-conventions.md``, "Snapshot, don't
reference, for historical records").

``plan_id`` is nullable with ``ON DELETE SET NULL`` so a deleted plan leaves its
revenue history intact rather than cascading it away, and so the backfill below can
leave a row unattributed rather than guessing.

**The backfill is best effort and is only correct for subscribers who never changed
plan.** Existing rows are set to their subscription's current plan, which is the only
information the database holds; re-deriving the true plan would mean re-reading every
invoice's price id from Stripe. New invoices are recorded correctly from this point.

Revision ID: a3d7f1b9c5e2
Revises: e1a8d364e817
Create Date: 2026-10-03 02:10:44.182703
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3d7f1b9c5e2"
down_revision: str | None = "e1a8d364e817"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscription_invoices",
        sa.Column("plan_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscription_invoices_plan_id",
        "subscription_invoices",
        "pricing_plans",
        ["plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_subscription_invoices_plan_id",
        "subscription_invoices",
        ["plan_id"],
    )
    # Best effort for history: the subscription's current plan is all we hold.
    op.execute(
        """
        UPDATE subscription_invoices AS si
           SET plan_id = s.plan_id
          FROM subscriptions AS s
         WHERE s.id = si.subscription_id
           AND si.plan_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_invoices_plan_id", table_name="subscription_invoices")
    op.drop_constraint(
        "fk_subscription_invoices_plan_id",
        "subscription_invoices",
        type_="foreignkey",
    )
    op.drop_column("subscription_invoices", "plan_id")
