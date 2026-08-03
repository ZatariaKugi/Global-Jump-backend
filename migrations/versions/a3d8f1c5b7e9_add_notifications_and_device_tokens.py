"""add_notifications_and_device_tokens

Revision ID: a3d8f1c5b7e9
Revises: f7a9c2b4d6e8
Create Date: 2026-07-31 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "a3d8f1c5b7e9"
down_revision: str | None = "f7a9c2b4d6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEVICE_PLATFORM = PgEnum("ios", "android", "web", name="device_platform", create_type=False)
_ENTITY_TYPE = PgEnum(
    "booking",
    "transaction",
    "payout_request",
    "user",
    "conversation",
    name="notification_entity_type",
    create_type=False,
)
_PUSH_STATUS = PgEnum(
    "pending", "sent", "failed", "skipped", name="notification_push_status", create_type=False
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
    _create_enum_safe("device_platform", "'ios','android','web'")
    _create_enum_safe(
        "notification_entity_type", "'booking','transaction','payout_request','user','conversation'"
    )
    _create_enum_safe("notification_push_status", "'pending','sent','failed','skipped'")

    op.create_table(
        "device_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column("platform", _DEVICE_PLATFORM, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(op.f("ix_device_tokens_user_id"), "device_tokens", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_device_tokens_is_archived"), "device_tokens", ["is_archived"], unique=False
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=False),
        sa.Column("entity_type", _ENTITY_TYPE, nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_status", _PUSH_STATUS, server_default="pending", nullable=False),
        sa.Column("push_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("push_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_last_error", sa.String(length=500), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_notifications_is_archived"), "notifications", ["is_archived"], unique=False
    )
    op.create_index(
        "ix_notifications_push_status_push_next_attempt_at",
        "notifications",
        ["push_status", "push_next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_user_id_created_at",
        "notifications",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_index("ix_notifications_push_status_push_next_attempt_at", table_name="notifications")
    op.drop_index(op.f("ix_notifications_is_archived"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(op.f("ix_device_tokens_is_archived"), table_name="device_tokens")
    op.drop_index(op.f("ix_device_tokens_user_id"), table_name="device_tokens")
    op.drop_table("device_tokens")
    op.execute(sa.text("DROP TYPE IF EXISTS notification_push_status"))
    op.execute(sa.text("DROP TYPE IF EXISTS notification_entity_type"))
    op.execute(sa.text("DROP TYPE IF EXISTS device_platform"))
