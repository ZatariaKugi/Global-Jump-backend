"""Stripe credentials on platform_payment_settings (EPIC 04, PAY-107 / PAY-108).

Moves the three Stripe values out of the environment and behind the admin panel.
The two secrets are stored as AES-256-GCM ciphertext produced by
``app/core/encryption.py``; the publishable key is public by definition and is
stored as it is.

All three are nullable and start empty, so an existing deployment keeps reading its
``.env`` values until an admin saves something — the resolver in
``stripe_config_service`` falls back to the environment per field.

Autogenerate also proposed dropping ``server_default`` from seven unrelated columns
(assessment_ab_variants, plan_feature_catalog, pre_registrations,
pricing_plan_features, stripe_webhook_events, subscriptions, transaction_refunds).
Those are artefacts of the models not declaring the defaults the database already
has; applying them would quietly change behaviour for tables this ticket does not
touch, so they were removed.

Revision ID: e1a8d364e817
Revises: c9e1a3b5d7f9
Create Date: 2026-09-30 00:07:36.896276
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a8d364e817"
down_revision: str | None = "c9e1a3b5d7f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_payment_settings",
        sa.Column("stripe_secret_key_enc", sa.Text(), nullable=True),
    )
    op.add_column(
        "platform_payment_settings",
        sa.Column("stripe_webhook_secret_enc", sa.Text(), nullable=True),
    )
    op.add_column(
        "platform_payment_settings",
        sa.Column("stripe_publishable_key", sa.String(length=255), nullable=True),
    )
    # "test" or "live", declared by the admin rather than inferred from a key prefix.
    op.add_column(
        "platform_payment_settings",
        sa.Column("stripe_mode", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_payment_settings", "stripe_mode")
    op.drop_column("platform_payment_settings", "stripe_publishable_key")
    op.drop_column("platform_payment_settings", "stripe_webhook_secret_enc")
    op.drop_column("platform_payment_settings", "stripe_secret_key_enc")
