"""add seeker_advisor_recommendations

Revision ID: f1a2b3c4d5e6
Revises: e4f6a8b0c2d1
Create Date: 2026-08-15 02:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e4f6a8b0c2d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seeker_advisor_recommendations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seeker_id", sa.Uuid(), nullable=False),
        sa.Column("advisor_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=True),
        sa.Column("destination_country", sa.String(length=2), nullable=False),
        sa.Column("visa_type", sa.String(length=50), nullable=False),
        sa.Column("context_source", sa.String(length=20), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=False),
        sa.Column("rule_score", sa.Float(), nullable=True),
        sa.Column("ai_score", sa.Float(), nullable=True),
        sa.Column("match_reasons", sa.String(length=1000), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["advisor_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["seeker_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seeker_id", "advisor_id"),
    )
    op.create_index(
        op.f("ix_seeker_advisor_recommendations_advisor_id"),
        "seeker_advisor_recommendations",
        ["advisor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_seeker_advisor_recommendations_assessment_id"),
        "seeker_advisor_recommendations",
        ["assessment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_seeker_advisor_recommendations_is_archived"),
        "seeker_advisor_recommendations",
        ["is_archived"],
        unique=False,
    )
    op.create_index(
        op.f("ix_seeker_advisor_recommendations_seeker_id"),
        "seeker_advisor_recommendations",
        ["seeker_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_seeker_advisor_recommendations_seeker_id"),
        table_name="seeker_advisor_recommendations",
    )
    op.drop_index(
        op.f("ix_seeker_advisor_recommendations_is_archived"),
        table_name="seeker_advisor_recommendations",
    )
    op.drop_index(
        op.f("ix_seeker_advisor_recommendations_assessment_id"),
        table_name="seeker_advisor_recommendations",
    )
    op.drop_index(
        op.f("ix_seeker_advisor_recommendations_advisor_id"),
        table_name="seeker_advisor_recommendations",
    )
    op.drop_table("seeker_advisor_recommendations")
