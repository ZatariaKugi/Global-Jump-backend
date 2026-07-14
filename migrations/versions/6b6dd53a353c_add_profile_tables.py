"""add_profile_tables

Revision ID: 6b6dd53a353c
Revises: 1ffd4bb07ae5
Create Date: 2026-06-09 21:43:26.171852

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

# revision identifiers, used by Alembic.
revision: str = "6b6dd53a353c"
down_revision: str | None = "1ffd4bb07ae5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reusable helper: creates the PG enum type via raw DDL (IF NOT EXISTS),
# then returns a PgEnum(..., create_type=False) instance for use in
# op.create_table so the before_create hook does NOT fire a second CREATE TYPE.
_DOC_TYPE = PgEnum(
    "immigration_license",
    "bar_membership",
    "certification",
    "government_id",
    "other",
    name="document_type",
    create_type=False,
)
_CRED_STATUS = PgEnum(
    "pending",
    "verified",
    "rejected",
    "expired",
    name="credential_status",
    create_type=False,
)
_EDU_LEVEL = PgEnum(
    "high_school",
    "bachelor",
    "master",
    "phd",
    "other",
    name="education_level",
    create_type=False,
)
_EMP_STATUS = PgEnum(
    "employed",
    "self_employed",
    "student",
    "unemployed",
    "retired",
    name="employment_status",
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
        "document_type",
        "'immigration_license','bar_membership','certification','government_id','other'",
    )
    _create_enum_safe("credential_status", "'pending','verified','rejected','expired'")
    _create_enum_safe("education_level", "'high_school','bachelor','master','phd','other'")
    _create_enum_safe(
        "employment_status", "'employed','self_employed','student','unemployed','retired'"
    )

    op.create_table(
        "advisor_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", _DOC_TYPE, nullable=False),
        sa.Column("document_name", sa.String(length=255), nullable=False),
        sa.Column("file_url", sa.String(length=500), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("status", _CRED_STATUS, nullable=False),
        sa.Column("admin_note", sa.String(length=1000), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
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
    op.create_index(
        op.f("ix_advisor_credentials_is_archived"),
        "advisor_credentials",
        ["is_archived"],
        unique=False,
    )
    op.create_index(
        op.f("ix_advisor_credentials_user_id"),
        "advisor_credentials",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "advisor_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=True),
        sa.Column("bio", sa.String(length=1000), nullable=True),
        sa.Column("profile_photo_url", sa.String(length=500), nullable=True),
        sa.Column("years_of_experience", sa.Integer(), nullable=True),
        sa.Column("successful_applications", sa.Integer(), nullable=True),
        sa.Column("visa_specializations", sa.JSON(), nullable=False),
        sa.Column("country_expertise", sa.JSON(), nullable=False),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("services", sa.JSON(), nullable=False),
        sa.Column("is_featured", sa.Boolean(), nullable=False),
        sa.Column("public_profile_slug", sa.String(length=100), nullable=True),
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
        sa.UniqueConstraint("public_profile_slug"),
    )
    op.create_index(
        op.f("ix_advisor_profiles_is_archived"),
        "advisor_profiles",
        ["is_archived"],
        unique=False,
    )
    op.create_index(
        op.f("ix_advisor_profiles_user_id"),
        "advisor_profiles",
        ["user_id"],
        unique=True,
    )

    op.create_table(
        "customer_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("nationality", sa.String(length=2), nullable=True),
        sa.Column("country_of_residence", sa.String(length=2), nullable=True),
        sa.Column("profile_photo_url", sa.String(length=500), nullable=True),
        sa.Column("passport_number_encrypted", sa.String(length=500), nullable=True),
        sa.Column("passport_expiry", sa.Date(), nullable=True),
        sa.Column("countries_visited", sa.JSON(), nullable=False),
        sa.Column("prior_visas", sa.JSON(), nullable=False),
        sa.Column("education_level", _EDU_LEVEL, nullable=True),
        sa.Column("employment_status", _EMP_STATUS, nullable=True),
        sa.Column("employer_name", sa.String(length=255), nullable=True),
        sa.Column("annual_income_band", sa.String(length=50), nullable=True),
        sa.Column("has_bank_statements", sa.Boolean(), nullable=False),
        sa.Column("email_notifications", sa.Boolean(), nullable=False),
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
    op.create_index(
        op.f("ix_customer_profiles_is_archived"),
        "customer_profiles",
        ["is_archived"],
        unique=False,
    )
    op.create_index(
        op.f("ix_customer_profiles_user_id"),
        "customer_profiles",
        ["user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_customer_profiles_user_id"), table_name="customer_profiles")
    op.drop_index(op.f("ix_customer_profiles_is_archived"), table_name="customer_profiles")
    op.drop_table("customer_profiles")

    op.drop_index(op.f("ix_advisor_profiles_user_id"), table_name="advisor_profiles")
    op.drop_index(op.f("ix_advisor_profiles_is_archived"), table_name="advisor_profiles")
    op.drop_table("advisor_profiles")

    op.drop_index(op.f("ix_advisor_credentials_user_id"), table_name="advisor_credentials")
    op.drop_index(op.f("ix_advisor_credentials_is_archived"), table_name="advisor_credentials")
    op.drop_table("advisor_credentials")

    op.execute(sa.text("DROP TYPE IF EXISTS employment_status"))
    op.execute(sa.text("DROP TYPE IF EXISTS education_level"))
    op.execute(sa.text("DROP TYPE IF EXISTS credential_status"))
    op.execute(sa.text("DROP TYPE IF EXISTS document_type"))
