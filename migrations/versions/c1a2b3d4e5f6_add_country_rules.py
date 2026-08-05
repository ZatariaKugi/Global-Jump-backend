"""add_country_rules

Revision ID: c1a2b3d4e5f6
Revises: b4c6e8d0f2a1
Create Date: 2026-08-06 00:00:00.000000

Creates the Country Rules & Policies tables: country_rules (versioned per
country/visa) plus three child tables, the rule_publish_status enum, the
version unique constraint, and a partial unique index enforcing at most one
published policy per (country, visa).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "c1a2b3d4e5f6"
down_revision: str | None = "b4c6e8d0f2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RULE_PUBLISH_STATUS = PgEnum(
    "generating",
    "draft",
    "published",
    "archived",
    name="rule_publish_status",
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


def _child_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("country_rule_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(length=1000), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["country_rule_id"], ["country_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f(f"ix_{name}_country_rule_id"), name, ["country_rule_id"], unique=False)


def upgrade() -> None:
    _create_enum_safe("rule_publish_status", "'generating','draft','published','archived'")

    op.create_table(
        "country_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("visa_type", sa.String(length=50), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _RULE_PUBLISH_STATUS, server_default="generating", nullable=False),
        sa.Column("summary", sa.String(length=4000), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("retrieved_url", sa.String(length=1000), nullable=True),
        sa.Column("grounded", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("generated_by_model", sa.String(length=100), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("country_code", "visa_type", "version", name="uq_country_rule_version"),
    )
    op.create_index(
        op.f("ix_country_rules_country_code"), "country_rules", ["country_code"], unique=False
    )
    op.create_index(
        op.f("ix_country_rules_visa_type"), "country_rules", ["visa_type"], unique=False
    )
    op.create_index(op.f("ix_country_rules_status"), "country_rules", ["status"], unique=False)
    op.create_index(
        op.f("ix_country_rules_is_archived"), "country_rules", ["is_archived"], unique=False
    )
    # Partial unique index: at most one published policy per (country, visa).
    op.create_index(
        "uq_country_rule_published",
        "country_rules",
        ["country_code", "visa_type"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )

    _child_table("country_rule_requirements")
    _child_table("country_rule_pitfalls")
    _child_table("country_rule_process_notes")


def downgrade() -> None:
    for name in (
        "country_rule_process_notes",
        "country_rule_pitfalls",
        "country_rule_requirements",
    ):
        op.drop_index(op.f(f"ix_{name}_country_rule_id"), table_name=name)
        op.drop_table(name)

    op.drop_index("uq_country_rule_published", table_name="country_rules")
    op.drop_index(op.f("ix_country_rules_is_archived"), table_name="country_rules")
    op.drop_index(op.f("ix_country_rules_status"), table_name="country_rules")
    op.drop_index(op.f("ix_country_rules_visa_type"), table_name="country_rules")
    op.drop_index(op.f("ix_country_rules_country_code"), table_name="country_rules")
    op.drop_table("country_rules")
    op.execute(sa.text("DROP TYPE IF EXISTS rule_publish_status"))
