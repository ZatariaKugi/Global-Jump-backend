"""Add Zoom connections, advisor integration flags, and booking meeting URLs.

Revision ID: c5e7a9b1d3f4
Revises: b2c3d4e5f6a7
Create Date: 2026-08-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5e7a9b1d3f4"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_enum_safe(name: str, values: str) -> None:
    """Create a PG enum type, silently skipping if it already exists."""
    op.execute(
        sa.text(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({values}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(col["name"] == column for col in insp.get_columns(table))


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    _create_enum_safe(
        "zoom_connection_status",
        "'connected','disconnected','revoked','error'",
    )

    status_enum = postgresql.ENUM(
        "connected",
        "disconnected",
        "revoked",
        "error",
        name="zoom_connection_status",
        create_type=False,
    )

    if not _table_exists("zoom_connections"):
        op.create_table(
            "zoom_connections",
            sa.Column("advisor_id", sa.Uuid(), nullable=False),
            sa.Column("zoom_user_id", sa.String(length=100), nullable=False),
            sa.Column("zoom_account_id", sa.String(length=100), nullable=True),
            sa.Column("zoom_email", sa.String(length=255), nullable=True),
            sa.Column("access_token_encrypted", sa.Text(), nullable=False),
            sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
            sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("scopes", sa.String(length=500), nullable=True),
            sa.Column("status", status_enum, nullable=False),
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("id", sa.Uuid(), nullable=False),
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
            sa.Column(
                "is_archived",
                sa.Boolean(),
                server_default="false",
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["advisor_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("advisor_id", name="uq_zoom_connections_advisor_id"),
        )
        op.create_index(
            op.f("ix_zoom_connections_advisor_id"),
            "zoom_connections",
            ["advisor_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_zoom_connections_is_archived"),
            "zoom_connections",
            ["is_archived"],
            unique=False,
        )

    if not _column_exists("advisor_profiles", "needs_stripe_connect"):
        op.add_column(
            "advisor_profiles",
            sa.Column(
                "needs_stripe_connect",
                sa.Boolean(),
                server_default="true",
                nullable=False,
            ),
        )
    if not _column_exists("advisor_profiles", "needs_zoom_connect"):
        op.add_column(
            "advisor_profiles",
            sa.Column(
                "needs_zoom_connect",
                sa.Boolean(),
                server_default="true",
                nullable=False,
            ),
        )

    op.execute(
        sa.text(
            """
            UPDATE advisor_profiles
            SET needs_stripe_connect = NOT (
                stripe_account_id IS NOT NULL
                AND stripe_charges_enabled
                AND stripe_payouts_enabled
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE advisor_profiles ap
            SET needs_zoom_connect = NOT EXISTS (
                SELECT 1 FROM zoom_connections zc
                WHERE zc.advisor_id = ap.user_id
                  AND zc.is_archived = false
                  AND zc.status = 'connected'
            )
            """
        )
    )

    if not _column_exists("bookings", "meeting_join_url"):
        op.add_column(
            "bookings",
            sa.Column("meeting_join_url", sa.String(length=500), nullable=True),
        )
    if not _column_exists("bookings", "meeting_start_url"):
        op.add_column(
            "bookings",
            sa.Column("meeting_start_url", sa.String(length=500), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("bookings", "meeting_start_url"):
        op.drop_column("bookings", "meeting_start_url")
    if _column_exists("bookings", "meeting_join_url"):
        op.drop_column("bookings", "meeting_join_url")
    if _column_exists("advisor_profiles", "needs_zoom_connect"):
        op.drop_column("advisor_profiles", "needs_zoom_connect")
    if _column_exists("advisor_profiles", "needs_stripe_connect"):
        op.drop_column("advisor_profiles", "needs_stripe_connect")
    if _table_exists("zoom_connections"):
        op.drop_index(op.f("ix_zoom_connections_is_archived"), table_name="zoom_connections")
        op.drop_index(op.f("ix_zoom_connections_advisor_id"), table_name="zoom_connections")
        op.drop_table("zoom_connections")
    op.execute(sa.text("DROP TYPE IF EXISTS zoom_connection_status"))
