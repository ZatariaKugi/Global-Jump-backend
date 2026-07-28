"""Merge seeker-document head with advisor banner head.

Revision ID: a1b2c3d4e5f7
Revises: f3a5b7c9d0e2, b9c1d3e5f7a9
Create Date: 2026-07-28

Joins the documents/expires chain with the client advisor banner_url branch
so ``alembic upgrade head`` has a single tip.
"""

from __future__ import annotations

from alembic import op

revision = "a1b2c3d4e5f7"
down_revision = ("f3a5b7c9d0e2", "b9c1d3e5f7a9")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both parents already applied schema changes on their branches.
    pass


def downgrade() -> None:
    pass
