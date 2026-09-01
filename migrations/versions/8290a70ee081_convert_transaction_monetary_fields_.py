"""convert_transaction monetary fields from float to numeric

Revision ID: 8290a70ee081
Revises: f1a2b3c4d5e6
Create Date: 2026-08-27 03:13:55.298223

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8290a70ee081"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "transactions",
        "amount_usd",
        existing_type=sa.Float(),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "commission_rate",
        existing_type=sa.Float(),
        type_=sa.Numeric(5, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "commission_usd",
        existing_type=sa.Float(),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "tax_rate",
        existing_type=sa.Float(),
        type_=sa.Numeric(5, 4),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "tax_usd",
        existing_type=sa.Float(),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "advisor_payout_usd",
        existing_type=sa.Float(),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "refunded_amount_usd",
        existing_type=sa.Float(),
        type_=sa.Numeric(10, 2),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "transactions",
        "refunded_amount_usd",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Float(),
        existing_nullable=True,
    )
    op.alter_column(
        "transactions",
        "advisor_payout_usd",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Float(),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "tax_usd",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Float(),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "tax_rate",
        existing_type=sa.Numeric(5, 4),
        type_=sa.Float(),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "commission_usd",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Float(),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "commission_rate",
        existing_type=sa.Numeric(5, 4),
        type_=sa.Float(),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "amount_usd",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Float(),
        existing_nullable=False,
    )
