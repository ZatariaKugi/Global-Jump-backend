"""Re-credit upgrade invoices to the plan they bought (QA BUG5, 2026-10-22).

Every upgrade-difference invoice written before this change carries the plan the
subscriber was LEAVING: the row was stamped with the subscription's current plan at
the moment ``invoice.paid`` arrived, which is before ``change_plan`` moved them. The
description our code wrote, ``Upgrade to <plan>``, names the plan the money was for,
so move each such row to that plan within the audience of the subscription's current
plan (plan names repeat across audiences). Rows naming no known plan are left alone.

Revision ID: a9c3d7e1f5b2
Revises: f8b2c6d0e4f7
Create Date: 2026-10-22
"""

from alembic import op

revision = "a9c3d7e1f5b2"
down_revision = "f8b2c6d0e4f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE subscription_invoices AS i
        SET plan_id = target.id
        FROM subscriptions AS s
        JOIN pricing_plans AS current_plan ON current_plan.id = s.plan_id
        JOIN pricing_plans AS target ON target.audience = current_plan.audience
        WHERE i.subscription_id = s.id
          AND i.description = 'Upgrade to ' || target.name
          AND i.plan_id IS DISTINCT FROM target.id
        """
    )


def downgrade() -> None:
    # The previous attribution was the defect; there is nothing to restore.
    pass
