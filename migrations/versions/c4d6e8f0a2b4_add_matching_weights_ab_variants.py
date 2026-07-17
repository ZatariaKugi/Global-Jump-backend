"""add_matching_weights_ab_variants_eligibility_category

Revision ID: c4d6e8f0a2b4
Revises: b3c5d7e9f1a2
Create Date: 2026-07-17 23:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d6e8f0a2b4"
down_revision: str | None = "b3c5d7e9f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

eligibility_rule_category = postgresql.ENUM(
    "age",
    "education",
    "work_experience",
    "language_proficiency",
    "other",
    name="eligibility_rule_category",
    create_type=False,
)


def upgrade() -> None:
    eligibility_rule_category.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "eligibility_rules",
        sa.Column(
            "category",
            eligibility_rule_category,
            server_default="other",
            nullable=False,
        ),
    )

    op.create_table(
        "advisor_matching_weights",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("country_weight", sa.Float(), nullable=False),
        sa.Column("language_weight", sa.Float(), nullable=False),
        sa.Column("availability_weight", sa.Float(), nullable=False),
        sa.Column("setting_weight", sa.Float(), nullable=False),
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
        op.f("ix_advisor_matching_weights_is_archived"),
        "advisor_matching_weights",
        ["is_archived"],
        unique=False,
    )

    op.create_table(
        "assessment_ab_variants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("visa_type", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
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
        op.f("ix_assessment_ab_variants_country_code"),
        "assessment_ab_variants",
        ["country_code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assessment_ab_variants_visa_type"),
        "assessment_ab_variants",
        ["visa_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assessment_ab_variants_is_archived"),
        "assessment_ab_variants",
        ["is_archived"],
        unique=False,
    )

    op.add_column("assessments", sa.Column("ab_variant_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_assessments_ab_variant_id"),
        "assessments",
        ["ab_variant_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_assessments_ab_variant_id_assessment_ab_variants"),
        "assessments",
        "assessment_ab_variants",
        ["ab_variant_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_assessments_ab_variant_id_assessment_ab_variants"),
        "assessments",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_assessments_ab_variant_id"), table_name="assessments")
    op.drop_column("assessments", "ab_variant_id")

    op.drop_index(
        op.f("ix_assessment_ab_variants_is_archived"),
        table_name="assessment_ab_variants",
    )
    op.drop_index(op.f("ix_assessment_ab_variants_visa_type"), table_name="assessment_ab_variants")
    op.drop_index(
        op.f("ix_assessment_ab_variants_country_code"),
        table_name="assessment_ab_variants",
    )
    op.drop_table("assessment_ab_variants")

    op.drop_index(
        op.f("ix_advisor_matching_weights_is_archived"),
        table_name="advisor_matching_weights",
    )
    op.drop_table("advisor_matching_weights")

    op.drop_column("eligibility_rules", "category")
    eligibility_rule_category.drop(op.get_bind(), checkfirst=True)
