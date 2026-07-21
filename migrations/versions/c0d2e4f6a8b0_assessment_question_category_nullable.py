"""Make assessment_questions.category nullable.

Revision ID: c0d2e4f6a8b0
Revises: b9c1d3e5f7a9
Create Date: 2026-07-21

Admin create may omit category when questions are scoped by country/visa_type.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c0d2e4f6a8b0"
down_revision = "b9c1d3e5f7a9"
branch_labels = None
depends_on = None

_QUESTION_CATEGORY = postgresql.ENUM(
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


def upgrade() -> None:
    op.alter_column(
        "assessment_questions",
        "category",
        existing_type=_QUESTION_CATEGORY,
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE assessment_questions SET category = 'purpose' WHERE category IS NULL"
        )
    )
    op.alter_column(
        "assessment_questions",
        "category",
        existing_type=_QUESTION_CATEGORY,
        nullable=False,
    )
