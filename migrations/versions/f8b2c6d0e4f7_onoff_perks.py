"""onoff_perks

Four seeker perks stop being numbers (QA GJ-SUB-010, PM 2026-10-15): assessment_history,
bookmarks, documents and matched_advisors are On/Off. A stored number meant "some
allowance", which now means On. The catalog rows themselves are healed on read.

Revision ID: f8b2c6d0e4f7
Revises: f7a1b5c9d3e6
Create Date: 2026-10-15 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8b2c6d0e4f7"
down_revision: str | None = "f7a1b5c9d3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEYS = "('assessment_history', 'bookmarks', 'documents', 'matched_advisors')"


def upgrade() -> None:
    op.execute(
        "UPDATE pricing_plan_features SET value_type = 'bool', value = 'true' "
        f"WHERE feature_key IN {KEYS} AND value_type = 'number'"
    )
    op.execute(
        "UPDATE subscription_features SET value_type = 'bool', value = 'true' "
        f"WHERE feature_key IN {KEYS} AND value_type = 'number'"
    )
    op.execute(
        "UPDATE plan_feature_catalog SET limit_type = 'bool' "
        f"WHERE key IN {KEYS} AND limit_type = 'number'"
    )
    op.execute(
        "UPDATE plan_feature_catalog SET label = 'AI recommendations' WHERE key = 'matched_advisors'"
    )
    # The card label is copied onto each plan row and snapshot at save time; rename the
    # ones that still carry the catalog's old wording (an admin's own wording is kept).
    for table in ("pricing_plan_features", "subscription_features"):
        op.execute(
            f"UPDATE {table} SET label = 'AI recommendations' "
            "WHERE feature_key = 'matched_advisors' AND label = 'Recommended advisors'"
        )


def downgrade() -> None:
    pass  # a number cannot be recovered from On; the catalog heals itself either way
