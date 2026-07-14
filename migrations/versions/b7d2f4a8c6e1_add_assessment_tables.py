"""add_assessment_tables

Revision ID: b7d2f4a8c6e1
Revises: a1c3e7f9b2d4
Create Date: 2026-06-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "b7d2f4a8c6e1"
down_revision: str | None = "a1c3e7f9b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_QUESTION_CATEGORY = PgEnum(
    "nationality",
    "travel_history",
    "financial",
    "education",
    "employment",
    "criminal_record",
    "visa_refusals",
    "family_ties",
    "language",
    "purpose",
    name="question_category",
    create_type=False,
)
_ASSESSMENT_STATUS = PgEnum(
    "in_progress",
    "completed",
    name="assessment_status",
    create_type=False,
)
_ELIGIBILITY_TIER = PgEnum(
    "highly_eligible",
    "likely_eligible",
    "borderline",
    "low_eligibility",
    name="eligibility_tier",
    create_type=False,
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
    _create_enum_safe(
        "question_category",
        "'nationality','travel_history','financial','education','employment',"
        "'criminal_record','visa_refusals','family_ties','language','purpose'",
    )
    _create_enum_safe("assessment_status", "'in_progress','completed'")
    _create_enum_safe(
        "eligibility_tier",
        "'highly_eligible','likely_eligible','borderline','low_eligibility'",
    )

    op.create_table(
        "assessment_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("category", _QUESTION_CATEGORY, nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("visa_type", sa.String(length=50), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("depends_on_option_id", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assessment_questions_country_code"),
        "assessment_questions",
        ["country_code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assessment_questions_visa_type"),
        "assessment_questions",
        ["visa_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assessment_questions_is_archived"),
        "assessment_questions",
        ["is_archived"],
        unique=False,
    )

    op.create_table(
        "assessment_question_options",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(length=255), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("improvement_tip", sa.String(length=500), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["assessment_questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assessment_question_options_question_id"),
        "assessment_question_options",
        ["question_id"],
        unique=False,
    )

    op.create_table(
        "assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("destination_country", sa.String(length=2), nullable=False),
        sa.Column("visa_type", sa.String(length=50), nullable=False),
        sa.Column("status", _ASSESSMENT_STATUS, nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("tier", _ELIGIBILITY_TIER, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_assessments_user_id"), "assessments", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_assessments_is_archived"), "assessments", ["is_archived"], unique=False
    )

    op.create_table(
        "assessment_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("option_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["assessment_questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["option_id"], ["assessment_question_options.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assessment_answers_assessment_id"),
        "assessment_answers",
        ["assessment_id"],
        unique=False,
    )

    op.create_table(
        "assessment_category_scores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assessment_category_scores_assessment_id"),
        "assessment_category_scores",
        ["assessment_id"],
        unique=False,
    )

    op.create_table(
        "assessment_tips",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("tip", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assessment_tips_assessment_id"),
        "assessment_tips",
        ["assessment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_assessment_tips_assessment_id"), table_name="assessment_tips")
    op.drop_table("assessment_tips")
    op.drop_index(
        op.f("ix_assessment_category_scores_assessment_id"),
        table_name="assessment_category_scores",
    )
    op.drop_table("assessment_category_scores")
    op.drop_index(op.f("ix_assessment_answers_assessment_id"), table_name="assessment_answers")
    op.drop_table("assessment_answers")
    op.drop_index(op.f("ix_assessments_is_archived"), table_name="assessments")
    op.drop_index(op.f("ix_assessments_user_id"), table_name="assessments")
    op.drop_table("assessments")
    op.drop_index(
        op.f("ix_assessment_question_options_question_id"),
        table_name="assessment_question_options",
    )
    op.drop_table("assessment_question_options")
    op.drop_index(op.f("ix_assessment_questions_is_archived"), table_name="assessment_questions")
    op.drop_index(op.f("ix_assessment_questions_visa_type"), table_name="assessment_questions")
    op.drop_index(op.f("ix_assessment_questions_country_code"), table_name="assessment_questions")
    op.drop_table("assessment_questions")

    op.execute(sa.text("DROP TYPE IF EXISTS eligibility_tier"))
    op.execute(sa.text("DROP TYPE IF EXISTS assessment_status"))
    op.execute(sa.text("DROP TYPE IF EXISTS question_category"))
