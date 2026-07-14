"""normalise_profile_json_to_tables

Revision ID: a1c3e7f9b2d4
Revises: 6b6dd53a353c
Create Date: 2026-06-09 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e7f9b2d4"
down_revision: str | None = "6b6dd53a353c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Customer travel history ──────────────────────────────────────────────
    op.create_table(
        "customer_countries_visited",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["customer_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_customer_countries_visited_profile_id"),
        "customer_countries_visited",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "customer_prior_visas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("visa_type", sa.String(length=100), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["customer_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_customer_prior_visas_profile_id"),
        "customer_prior_visas",
        ["profile_id"],
        unique=False,
    )

    # Drop JSON columns from customer_profiles
    op.drop_column("customer_profiles", "countries_visited")
    op.drop_column("customer_profiles", "prior_visas")

    # ── Advisor expertise ────────────────────────────────────────────────────
    op.create_table(
        "advisor_visa_specializations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("specialization", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["advisor_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_advisor_visa_specializations_profile_id"),
        "advisor_visa_specializations",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "advisor_country_expertise",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["advisor_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_advisor_country_expertise_profile_id"),
        "advisor_country_expertise",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "advisor_languages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=100), nullable=False),
        sa.Column("proficiency", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["advisor_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_advisor_languages_profile_id"),
        "advisor_languages",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "advisor_services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("service_type", sa.String(length=100), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["advisor_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_advisor_services_profile_id"),
        "advisor_services",
        ["profile_id"],
        unique=False,
    )

    # Drop JSON columns from advisor_profiles
    op.drop_column("advisor_profiles", "visa_specializations")
    op.drop_column("advisor_profiles", "country_expertise")
    op.drop_column("advisor_profiles", "languages")
    op.drop_column("advisor_profiles", "services")


def downgrade() -> None:
    _j = sa.JSON()

    # Restore JSON columns on advisor_profiles
    op.add_column(
        "advisor_profiles",
        sa.Column("services", _j, nullable=False, server_default="[]"),
    )
    op.add_column(
        "advisor_profiles",
        sa.Column("languages", _j, nullable=False, server_default="[]"),
    )
    op.add_column(
        "advisor_profiles",
        sa.Column("country_expertise", _j, nullable=False, server_default="[]"),
    )
    op.add_column(
        "advisor_profiles",
        sa.Column("visa_specializations", _j, nullable=False, server_default="[]"),
    )

    op.drop_index(op.f("ix_advisor_services_profile_id"), table_name="advisor_services")
    op.drop_table("advisor_services")
    op.drop_index(op.f("ix_advisor_languages_profile_id"), table_name="advisor_languages")
    op.drop_table("advisor_languages")
    op.drop_index(
        op.f("ix_advisor_country_expertise_profile_id"),
        table_name="advisor_country_expertise",
    )
    op.drop_table("advisor_country_expertise")
    op.drop_index(
        op.f("ix_advisor_visa_specializations_profile_id"),
        table_name="advisor_visa_specializations",
    )
    op.drop_table("advisor_visa_specializations")

    # Restore JSON columns on customer_profiles
    op.add_column(
        "customer_profiles",
        sa.Column("prior_visas", _j, nullable=False, server_default="[]"),
    )
    op.add_column(
        "customer_profiles",
        sa.Column("countries_visited", _j, nullable=False, server_default="[]"),
    )

    op.drop_index(op.f("ix_customer_prior_visas_profile_id"), table_name="customer_prior_visas")
    op.drop_table("customer_prior_visas")
    op.drop_index(
        op.f("ix_customer_countries_visited_profile_id"),
        table_name="customer_countries_visited",
    )
    op.drop_table("customer_countries_visited")
