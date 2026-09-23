"""link advisor offered services to the catalog, and fold advisor_services into it

Adds ``catalog_id`` (RESTRICT) and ``name``, uniqueness on (profile_id,
service_type) plus a partial unique index for catalog rows, folds priced rows
from the legacy ``advisor_services`` table in, and backfills ``catalog_id``.
``advisor_services`` is left in place; it is dropped in a later revision.

Revision ID: d1f3a5b7c9e2
Revises: c3e5a7b9d1f2
Create Date: 2026-09-08 10:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3a5b7c9e2"
down_revision: str | None = "c3e5a7b9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors app.core.service_types.SERVICE_TYPE_ALIASES at the time this ran.
_ALIASES = {"immigration": "immigration_specialist"}


def upgrade() -> None:
    op.add_column(
        "advisor_offered_services",
        sa.Column("catalog_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "advisor_offered_services",
        sa.Column("name", sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f("ix_advisor_offered_services_catalog_id"),
        "advisor_offered_services",
        ["catalog_id"],
    )
    op.create_foreign_key(
        "fk_offered_service_catalog",
        "advisor_offered_services",
        "advisor_offered_services",
        ["catalog_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # De-duplicate before the unique constraints go on: keep the priced row.
    op.execute(
        """
        DELETE FROM advisor_offered_services a
        USING advisor_offered_services b
        WHERE a.profile_id IS NOT DISTINCT FROM b.profile_id
          AND a.service_type = b.service_type
          AND a.id <> b.id
          AND (
                (b.price_usd IS NOT NULL AND a.price_usd IS NULL)
             OR (
                  (b.price_usd IS NULL) = (a.price_usd IS NULL)
                  AND b.id < a.id
                )
          )
        """
    )

    # Fold legacy advisor_services in: update where already selected...
    op.execute(
        """
        UPDATE advisor_offered_services o
        SET price_usd = s.price_usd,
            duration_minutes = s.duration_minutes
        FROM advisor_services s
        WHERE o.profile_id = s.profile_id
          AND o.service_type = s.service_type
          AND o.price_usd IS NULL
        """
    )
    # ...and insert the ones they never selected.
    op.execute(
        """
        INSERT INTO advisor_offered_services
            (id, profile_id, service_type, price_usd, duration_minutes)
        SELECT gen_random_uuid(), s.profile_id, s.service_type,
               s.price_usd, s.duration_minutes
        FROM advisor_services s
        WHERE NOT EXISTS (
            SELECT 1 FROM advisor_offered_services o
            WHERE o.profile_id = s.profile_id
              AND o.service_type = s.service_type
        )
        """
    )

    # Seed catalog display names, then link advisor rows.
    op.execute(
        """
        UPDATE advisor_offered_services
        SET name = initcap(replace(service_type, '_', ' '))
        WHERE profile_id IS NULL AND name IS NULL
        """
    )
    op.execute(
        """
        UPDATE advisor_offered_services a
        SET catalog_id = c.id
        FROM advisor_offered_services c
        WHERE a.profile_id IS NOT NULL
          AND c.profile_id IS NULL
          AND a.catalog_id IS NULL
          AND c.service_type = a.service_type
        """
    )
    for legacy, canonical in _ALIASES.items():
        op.execute(
            sa.text(
                """
                UPDATE advisor_offered_services a
                SET catalog_id = c.id
                FROM advisor_offered_services c
                WHERE a.profile_id IS NOT NULL
                  AND c.profile_id IS NULL
                  AND a.catalog_id IS NULL
                  AND a.service_type = :legacy
                  AND c.service_type = :canonical
                """
            ).bindparams(legacy=legacy, canonical=canonical)
        )

    op.create_unique_constraint(
        "uq_offered_service_profile_type",
        "advisor_offered_services",
        ["profile_id", "service_type"],
    )
    op.create_index(
        "uq_offered_service_catalog_type",
        "advisor_offered_services",
        ["service_type"],
        unique=True,
        postgresql_where=sa.text("profile_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_offered_service_catalog_type", table_name="advisor_offered_services")
    op.drop_constraint(
        "uq_offered_service_profile_type", "advisor_offered_services", type_="unique"
    )
    op.drop_constraint("fk_offered_service_catalog", "advisor_offered_services", type_="foreignkey")
    op.drop_index(
        op.f("ix_advisor_offered_services_catalog_id"), table_name="advisor_offered_services"
    )
    op.drop_column("advisor_offered_services", "name")
    op.drop_column("advisor_offered_services", "catalog_id")
