"""add_reviews_table

Revision ID: d4f8b2c7e9a5
Revises: c9e4a6b1d8f3
Create Date: 2026-06-11 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "d4f8b2c7e9a5"
down_revision: str | None = "c9e4a6b1d8f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODERATION_STATUS = PgEnum(
    "visible",
    "flagged",
    "removed",
    name="moderation_status",
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
    _create_enum_safe("moderation_status", "'visible','flagged','removed'")

    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("advisor_id", sa.Uuid(), nullable=False),
        sa.Column("rating_expertise", sa.Integer(), nullable=False),
        sa.Column("rating_communication", sa.Integer(), nullable=False),
        sa.Column("rating_professionalism", sa.Integer(), nullable=False),
        sa.Column("rating_value", sa.Integer(), nullable=False),
        sa.Column("rating_overall", sa.Float(), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("advisor_response", sa.String(length=500), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("moderation_status", _MODERATION_STATUS, nullable=False),
        sa.Column("flag_reason", sa.String(length=500), nullable=True),
        sa.Column("flagged_by", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["advisor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id"),
    )
    op.create_index(op.f("ix_reviews_customer_id"), "reviews", ["customer_id"], unique=False)
    op.create_index(op.f("ix_reviews_advisor_id"), "reviews", ["advisor_id"], unique=False)
    op.create_index(op.f("ix_reviews_is_archived"), "reviews", ["is_archived"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_reviews_is_archived"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_advisor_id"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_customer_id"), table_name="reviews")
    op.drop_table("reviews")
    op.execute(sa.text("DROP TYPE IF EXISTS moderation_status"))
