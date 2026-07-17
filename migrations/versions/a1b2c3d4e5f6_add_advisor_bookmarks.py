"""add_advisor_bookmarks

Revision ID: a1b2c3d4e5f6
Revises: d8f0a2b4c6e8
Create Date: 2026-07-16 22:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d8f0a2b4c6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "advisor_bookmarks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seeker_id", sa.Uuid(), nullable=False),
        sa.Column("advisor_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["seeker_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seeker_id", "advisor_id"),
    )
    op.create_index(
        op.f("ix_advisor_bookmarks_advisor_id"),
        "advisor_bookmarks",
        ["advisor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_advisor_bookmarks_is_archived"),
        "advisor_bookmarks",
        ["is_archived"],
        unique=False,
    )
    op.create_index(
        op.f("ix_advisor_bookmarks_seeker_id"),
        "advisor_bookmarks",
        ["seeker_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_advisor_bookmarks_seeker_id"), table_name="advisor_bookmarks")
    op.drop_index(op.f("ix_advisor_bookmarks_is_archived"), table_name="advisor_bookmarks")
    op.drop_index(op.f("ix_advisor_bookmarks_advisor_id"), table_name="advisor_bookmarks")
    op.drop_table("advisor_bookmarks")
