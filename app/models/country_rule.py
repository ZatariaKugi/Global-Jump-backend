"""Admin-verified, country- and visa-specific immigration policy (Country Rules & Policies).

AI drafts a structured policy from an official government source; an admin reviews,
edits, and publishes it. Only ``published`` rows are consumed by the live AI
assessment. Policies are versioned per (country, visa) and never overwritten —
regeneration creates a new version and publishing archives the previous one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class RulePublishStatus(StrEnum):
    generating = "generating"  
    draft = "draft"
    published = "published"  
    archived = "archived"  


class CountryRuleRequirement(Base):
    """A typical requirement row for a policy."""

    __tablename__ = "country_rule_requirements"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    country_rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("country_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CountryRulePitfall(Base):
    """A common-pitfall row for a policy."""

    __tablename__ = "country_rule_pitfalls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    country_rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("country_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CountryRuleProcessNote(Base):
    """A process-note row for a policy."""

    __tablename__ = "country_rule_process_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    country_rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("country_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CountryRule(BaseModel):
    """One versioned immigration policy for a (country_code, visa_type) pair."""

    __tablename__ = "country_rules"

    __table_args__ = (
        # No two versions collide for the same pair.
        UniqueConstraint("country_code", "visa_type", "version", name="uq_country_rule_version"),
        # At most one *published* policy per (country, visa) — partial unique index.
        # SQLite (tests) also honours partial indexes via ``sqlite_where``.
        Index(
            "uq_country_rule_published",
            "country_code",
            "visa_type",
            unique=True,
            postgresql_where=sa_text("status = 'published'"),
            sqlite_where=sa_text("status = 'published'"),
        ),
    )

    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    visa_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RulePublishStatus] = mapped_column(
        SAEnum(RulePublishStatus, name="rule_publish_status"),
        default=RulePublishStatus.generating,
        server_default="generating",
        nullable=False,
        index=True,
    )

    summary: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    # Admin-verified source; required before publishing.
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # What the AI actually retrieved (provenance).
    retrieved_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # True only when the retrieved URL matched the expected official domain.
    grounded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    generated_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    requirements: Mapped[list[CountryRuleRequirement]] = relationship(
        "CountryRuleRequirement",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CountryRuleRequirement.display_order",
    )
    pitfalls: Mapped[list[CountryRulePitfall]] = relationship(
        "CountryRulePitfall",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CountryRulePitfall.display_order",
    )
    process_notes: Mapped[list[CountryRuleProcessNote]] = relationship(
        "CountryRuleProcessNote",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CountryRuleProcessNote.display_order",
    )
