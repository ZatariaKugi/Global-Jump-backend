"""add seeker document advisor reviews

Revision ID: b2d4f6a8c0e3
Revises: a1b2c3d4e5f8
Create Date: 2026-08-31 22:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "b2d4f6a8c0e3"
down_revision: str | None = "a1b2c3d4e5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reuses seeker_document_status from e1734162178e (+ expired from c2d4e6f8a0b1).
_SEEKER_DOCUMENT_STATUS = PgEnum(
    "under_review",
    "approved",
    "rejected",
    "expired",
    name="seeker_document_status",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "seeker_document_advisor_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("advisor_id", sa.Uuid(), nullable=False),
        sa.Column("status", _SEEKER_DOCUMENT_STATUS, nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=2000), nullable=True),
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
        sa.ForeignKeyConstraint(["document_id"], ["seeker_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "advisor_id", name="uq_doc_advisor_review"),
    )
    op.create_index(
        op.f("ix_seeker_document_advisor_reviews_advisor_id"),
        "seeker_document_advisor_reviews",
        ["advisor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_seeker_document_advisor_reviews_document_id"),
        "seeker_document_advisor_reviews",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_seeker_document_advisor_reviews_is_archived"),
        "seeker_document_advisor_reviews",
        ["is_archived"],
        unique=False,
    )

    # Backfill prior advisor decisions so existing reviewers keep their history.
    op.execute(
        sa.text(
            """
            INSERT INTO seeker_document_advisor_reviews (
                id, document_id, advisor_id, status, reviewed_at,
                created_at, updated_at, is_archived
            )
            SELECT
                gen_random_uuid(),
                id,
                reviewed_by,
                status,
                COALESCE(reviewed_at, now()),
                now(),
                now(),
                false
            FROM seeker_documents
            WHERE reviewed_by IS NOT NULL
              AND status IN ('approved', 'rejected')
              AND is_archived = false
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_seeker_document_advisor_reviews_is_archived"),
        table_name="seeker_document_advisor_reviews",
    )
    op.drop_index(
        op.f("ix_seeker_document_advisor_reviews_document_id"),
        table_name="seeker_document_advisor_reviews",
    )
    op.drop_index(
        op.f("ix_seeker_document_advisor_reviews_advisor_id"),
        table_name="seeker_document_advisor_reviews",
    )
    op.drop_table("seeker_document_advisor_reviews")
