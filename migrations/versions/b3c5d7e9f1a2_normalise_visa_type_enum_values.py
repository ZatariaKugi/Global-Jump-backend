"""Normalise stored visa-type values to the PRD VisaType enum.

Revision ID: b3c5d7e9f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-16

Maps legacy aliases (study, permanent_residency, …) to the closed set:
tourist, work, student, pr, family, investment, asylum.
Does not create a Postgres ENUM type — app-layer ``VisaType`` enforces writes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b3c5d7e9f1a2"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# legacy alias → canonical VisaType value
_REMAP: list[tuple[str, str]] = [
    ("study", "student"),
    ("study_visa", "student"),
    ("student_visa", "student"),
    ("permanent_residency", "pr"),
    ("work_visa", "work"),
    ("tourist_visa", "tourist"),
    ("family_sponsorship", "family"),
]

_COLUMNS: list[tuple[str, str]] = [
    ("advisor_visa_specializations", "specialization"),
    ("seeker_profiles", "intended_visa_type"),
    ("seeker_prior_visas", "visa_type"),
    ("assessments", "visa_type"),
    ("assessment_questions", "visa_type"),
    ("eligibility_rules", "visa_type"),
    ("assessment_thresholds", "visa_type"),
]


def upgrade() -> None:
    for table, column in _COLUMNS:
        for old, new in _REMAP:
            op.execute(
                f"UPDATE {table} SET {column} = '{new}' "
                f"WHERE lower({column}) = '{old}'"
            )
        # Drop unsupported legacy value ``other`` from specialization/intent columns.
        if table in {"advisor_visa_specializations"}:
            op.execute(
                f"DELETE FROM {table} WHERE lower({column}) = 'other'"
            )
        elif table == "seeker_profiles":
            op.execute(
                f"UPDATE {table} SET {column} = NULL WHERE lower({column}) = 'other'"
            )


def downgrade() -> None:
    # Best-effort reverse for the primary renames only.
    reverse = [
        ("student", "study"),
        ("pr", "permanent_residency"),
    ]
    for table, column in _COLUMNS:
        for new, old in reverse:
            op.execute(
                f"UPDATE {table} SET {column} = '{old}' "
                f"WHERE lower({column}) = '{new}'"
            )
