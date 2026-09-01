"""Seeker preferred languages + needed services; retarget matching weights."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f0a2b4c6d8e1"
down_revision = "e9f1a3b5c7d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seeker_preferred_languages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["seeker_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_seeker_preferred_languages_profile_id"),
        "seeker_preferred_languages",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "seeker_needed_services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("service_type", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["seeker_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_seeker_needed_services_profile_id"),
        "seeker_needed_services",
        ["profile_id"],
        unique=False,
    )

    # Migrate legacy single preferred_language into the child table.
    op.execute(
        """
        INSERT INTO seeker_preferred_languages (id, profile_id, language)
        SELECT gen_random_uuid(), id, preferred_language
        FROM seeker_profiles
        WHERE preferred_language IS NOT NULL AND btrim(preferred_language) <> ''
        """
    )
    op.drop_column("seeker_profiles", "preferred_language")

    # Retarget matching weights: country/visa/language/services/experience/rating.
    op.add_column(
        "advisor_matching_weights",
        sa.Column("visa_weight", sa.Float(), nullable=False, server_default="25"),
    )
    op.add_column(
        "advisor_matching_weights",
        sa.Column("services_weight", sa.Float(), nullable=False, server_default="15"),
    )
    op.add_column(
        "advisor_matching_weights",
        sa.Column("experience_weight", sa.Float(), nullable=False, server_default="10"),
    )
    op.add_column(
        "advisor_matching_weights",
        sa.Column("rating_weight", sa.Float(), nullable=False, server_default="10"),
    )
    op.execute(
        """
        UPDATE advisor_matching_weights
        SET
            country_weight = 25,
            language_weight = 15,
            visa_weight = COALESCE(setting_weight, 25),
            services_weight = 15,
            experience_weight = 10,
            rating_weight = 10
        """
    )
    op.drop_column("advisor_matching_weights", "availability_weight")
    op.drop_column("advisor_matching_weights", "setting_weight")
    op.alter_column("advisor_matching_weights", "visa_weight", server_default=None)
    op.alter_column("advisor_matching_weights", "services_weight", server_default=None)
    op.alter_column("advisor_matching_weights", "experience_weight", server_default=None)
    op.alter_column("advisor_matching_weights", "rating_weight", server_default=None)


def downgrade() -> None:
    op.add_column(
        "advisor_matching_weights",
        sa.Column("availability_weight", sa.Float(), nullable=False, server_default="20"),
    )
    op.add_column(
        "advisor_matching_weights",
        sa.Column("setting_weight", sa.Float(), nullable=False, server_default="20"),
    )
    op.execute(
        """
        UPDATE advisor_matching_weights
        SET
            country_weight = 40,
            language_weight = 20,
            availability_weight = 20,
            setting_weight = COALESCE(visa_weight, 20)
        """
    )
    op.drop_column("advisor_matching_weights", "visa_weight")
    op.drop_column("advisor_matching_weights", "services_weight")
    op.drop_column("advisor_matching_weights", "experience_weight")
    op.drop_column("advisor_matching_weights", "rating_weight")
    op.alter_column("advisor_matching_weights", "availability_weight", server_default=None)
    op.alter_column("advisor_matching_weights", "setting_weight", server_default=None)

    op.add_column(
        "seeker_profiles",
        sa.Column("preferred_language", sa.String(length=100), nullable=True),
    )
    op.execute(
        """
        UPDATE seeker_profiles sp
        SET preferred_language = sub.language
        FROM (
            SELECT DISTINCT ON (profile_id) profile_id, language
            FROM seeker_preferred_languages
            ORDER BY profile_id, language
        ) AS sub
        WHERE sp.id = sub.profile_id
        """
    )
    op.drop_index(
        op.f("ix_seeker_needed_services_profile_id"), table_name="seeker_needed_services"
    )
    op.drop_table("seeker_needed_services")
    op.drop_index(
        op.f("ix_seeker_preferred_languages_profile_id"),
        table_name="seeker_preferred_languages",
    )
    op.drop_table("seeker_preferred_languages")
