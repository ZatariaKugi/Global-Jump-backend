"""add_support_ticket_assignee_notes

Revision ID: d5e7f9a1b3c5
Revises: c4d6e8f0a2b4
Create Date: 2026-07-18 00:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e7f9a1b3c5"
down_revision: str | None = "c4d6e8f0a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("support_tickets", sa.Column("assigned_to", sa.Uuid(), nullable=True))
    op.add_column(
        "support_tickets", sa.Column("internal_notes", sa.String(length=2000), nullable=True)
    )
    op.create_index(
        op.f("ix_support_tickets_assigned_to"), "support_tickets", ["assigned_to"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_support_tickets_assigned_to_users"),
        "support_tickets",
        "users",
        ["assigned_to"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_support_tickets_assigned_to_users"), "support_tickets", type_="foreignkey"
    )
    op.drop_index(op.f("ix_support_tickets_assigned_to"), table_name="support_tickets")
    op.drop_column("support_tickets", "internal_notes")
    op.drop_column("support_tickets", "assigned_to")
