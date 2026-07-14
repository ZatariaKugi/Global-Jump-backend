"""add_assessment_ai_insights

Revision ID: c8d5b56d844d
Revises: 2b00b2b88648
Create Date: 2026-07-10 22:05:32.651475

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

# revision identifiers, used by Alembic.
revision: str = "c8d5b56d844d"
down_revision: str | None = "2b00b2b88648"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INSIGHT_KIND = PgEnum(
    "strength", "weakness", "missing_requirement", name="insight_kind", create_type=False
)


def _create_enum_safe(name: str, values: str) -> None:
    """Create a PG enum type, silently skipping if it already exists."""
    op.execute(
        sa.text(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({values}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )


def upgrade() -> None:
    _create_enum_safe("insight_kind", "'strength','weakness','missing_requirement'")
    op.create_table(
        "assessment_insights",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("kind", _INSIGHT_KIND, nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assessment_insights_assessment_id"),
        "assessment_insights",
        ["assessment_id"],
        unique=False,
    )
    op.add_column("assessments", sa.Column("ai_summary", sa.String(length=2000), nullable=True))


def downgrade() -> None:
    op.drop_column("assessments", "ai_summary")
    op.drop_index(op.f("ix_assessment_insights_assessment_id"), table_name="assessment_insights")
    op.drop_table("assessment_insights")
    op.execute(sa.text("DROP TYPE IF EXISTS insight_kind"))
