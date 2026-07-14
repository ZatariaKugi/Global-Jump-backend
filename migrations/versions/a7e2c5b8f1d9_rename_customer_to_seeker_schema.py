"""rename customer_* tables/columns to seeker_* for naming consistency

Revision ID: a7e2c5b8f1d9
Revises: f3a1b9c7d2e4
Create Date: 2026-06-24 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e2c5b8f1d9"
down_revision: str | None = "f3a1b9c7d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tables
    op.rename_table("customer_profiles", "seeker_profiles")
    op.rename_table("customer_countries_visited", "seeker_countries_visited")
    op.rename_table("customer_prior_visas", "seeker_prior_visas")

    # Columns
    op.alter_column("bookings", "customer_id", new_column_name="seeker_id")
    op.alter_column("bookings", "customer_note", new_column_name="seeker_note")
    op.alter_column("reviews", "customer_id", new_column_name="seeker_id")
    op.alter_column("conversations", "customer_id", new_column_name="seeker_id")

    # Indexes (renamed to match the new column/table names)
    op.execute(
        "ALTER INDEX ix_customer_profiles_is_archived RENAME TO ix_seeker_profiles_is_archived"
    )
    op.execute("ALTER INDEX ix_customer_profiles_user_id RENAME TO ix_seeker_profiles_user_id")
    op.execute(
        "ALTER INDEX ix_customer_countries_visited_profile_id "
        "RENAME TO ix_seeker_countries_visited_profile_id"
    )
    op.execute(
        "ALTER INDEX ix_customer_prior_visas_profile_id RENAME TO ix_seeker_prior_visas_profile_id"
    )
    op.execute("ALTER INDEX ix_bookings_customer_id RENAME TO ix_bookings_seeker_id")
    op.execute("ALTER INDEX ix_reviews_customer_id RENAME TO ix_reviews_seeker_id")
    op.execute("ALTER INDEX ix_conversations_customer_id RENAME TO ix_conversations_seeker_id")
    op.execute(
        "ALTER INDEX conversations_customer_id_advisor_id_key "
        "RENAME TO conversations_seeker_id_advisor_id_key"
    )


def downgrade() -> None:
    op.execute(
        "ALTER INDEX conversations_seeker_id_advisor_id_key "
        "RENAME TO conversations_customer_id_advisor_id_key"
    )
    op.execute("ALTER INDEX ix_conversations_seeker_id RENAME TO ix_conversations_customer_id")
    op.execute("ALTER INDEX ix_reviews_seeker_id RENAME TO ix_reviews_customer_id")
    op.execute("ALTER INDEX ix_bookings_seeker_id RENAME TO ix_bookings_customer_id")
    op.execute(
        "ALTER INDEX ix_seeker_prior_visas_profile_id RENAME TO ix_customer_prior_visas_profile_id"
    )
    op.execute(
        "ALTER INDEX ix_seeker_countries_visited_profile_id "
        "RENAME TO ix_customer_countries_visited_profile_id"
    )
    op.execute("ALTER INDEX ix_seeker_profiles_user_id RENAME TO ix_customer_profiles_user_id")
    op.execute(
        "ALTER INDEX ix_seeker_profiles_is_archived RENAME TO ix_customer_profiles_is_archived"
    )

    op.alter_column("conversations", "seeker_id", new_column_name="customer_id")
    op.alter_column("reviews", "seeker_id", new_column_name="customer_id")
    op.alter_column("bookings", "seeker_note", new_column_name="customer_note")
    op.alter_column("bookings", "seeker_id", new_column_name="customer_id")

    op.rename_table("seeker_prior_visas", "customer_prior_visas")
    op.rename_table("seeker_countries_visited", "customer_countries_visited")
    op.rename_table("seeker_profiles", "customer_profiles")
