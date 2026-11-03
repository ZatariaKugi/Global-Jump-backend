"""Relabel admin "new user" notifications for seekers as ``seeker_registered`` (#340).

Every sign-up notified the admins with ``user_registered`` and the admin's deep link sent
all of them to the Verification Queue, where a seeker never appears. New seeker sign-ups
now write ``seeker_registered``; this moves the existing seeker rows over (the actor is
the new user), so old notifications open the right screen too. Advisors keep
``user_registered``.

Revision ID: c7e1a3f5b9d2
Revises: a9c3d7e1f5b2
Create Date: 2026-10-30
"""

from alembic import op

revision = "c7e1a3f5b9d2"
down_revision = "a9c3d7e1f5b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE notifications AS n
        SET type = 'seeker_registered'
        FROM users AS u
        WHERE u.id = n.actor_id
          AND n.type = 'user_registered'
          AND u.role = 'seeker'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE notifications
        SET type = 'user_registered'
        WHERE type = 'seeker_registered'
        """
    )
