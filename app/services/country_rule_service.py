"""Country Rules & Policies service — generation, versioning, publishing, CRUD.

Trust model: AI drafts (untrusted) → admin verifies/edits → publish. Only
``published`` rows are read by live assessment. See §16–§19, §30.

Concurrency: ``generate()`` owns its own transaction boundaries (it does NOT use
the request session) so the ~90s web-search call holds no DB lock and the
``generating`` placeholder is committed — and thus visible to concurrent
requests — before the slow call runs. That makes the in-flight check a real
cross-request guard on both Postgres and SQLite; on Postgres a per-pair advisory
lock additionally closes the check-then-insert window.
"""

from __future__ import annotations

import uuid
import zlib
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.countries import is_supported_country
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.official_sources import official_source
from app.core.visa_types import parse_visa_type
from app.db.session import async_session_factory
from app.models.country_rule import (
    CountryRule,
    CountryRulePitfall,
    CountryRuleProcessNote,
    CountryRuleRequirement,
    RulePublishStatus,
)
from app.services import country_rule_ai_service


def _normalise_pair(country_code: str, visa_type: str) -> tuple[str, str]:
    code = (country_code or "").upper()
    if not is_supported_country(code):
        raise AppError(f"Unsupported country {country_code!r}", code="invalid_country")
    parsed = parse_visa_type(visa_type)
    if parsed is None:
        raise AppError(f"Invalid visa type {visa_type!r}", code="invalid_visa_type")
    if official_source(code) is None:
        raise AppError(f"No official source registered for {code}", code="no_official_source")
    return code, parsed.value


def _advisory_key(country_code: str, visa_type: str) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock from the pair."""
    raw = zlib.crc32(f"{country_code}:{visa_type}".encode())
    return int(raw)


async def _next_version(session: AsyncSession, country_code: str, visa_type: str) -> int:
    result = await session.execute(
        select(func.coalesce(func.max(CountryRule.version), 0)).where(
            CountryRule.country_code == country_code,
            CountryRule.visa_type == visa_type,
        )
    )
    return int(result.scalar_one()) + 1


async def generate(
    country_code: str,
    visa_type: str,
    admin_id: uuid.UUID,
    settings: Settings,
) -> uuid.UUID:
    """Generate a new AI draft for a (country, visa) pair. Returns the new rule id.

    Raises ``AppError`` on invalid input, ``ConflictError`` (409) when a
    generation is already in flight, or ``AppError`` when the AI call fails.
    """
    code, visa = _normalise_pair(country_code, visa_type)

    # ── Tx A: reserve the slot under the lock, then commit so the placeholder
    # is visible to other requests before the slow AI call. ──────────────────
    async with async_session_factory() as session:
        is_pg = session.bind.dialect.name == "postgresql"
        if is_pg:
            await session.execute(
                sa_text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _advisory_key(code, visa)},
            )

        in_flight = await session.execute(
            select(CountryRule.id).where(
                CountryRule.country_code == code,
                CountryRule.visa_type == visa,
                CountryRule.status == RulePublishStatus.generating,
            )
        )
        if in_flight.first() is not None:
            raise ConflictError("A draft is already being generated for this pair")

        version = await _next_version(session, code, visa)
        rule = CountryRule(
            country_code=code,
            visa_type=visa,
            version=version,
            status=RulePublishStatus.generating,
            created_by=admin_id,
            updated_by=admin_id,
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        await session.commit()  # releases advisory lock (xact) + publishes placeholder

    # ── Slow AI call: no lock held, no open transaction. ──────────────────────
    draft = await country_rule_ai_service.draft_policy(code, visa, settings)

    # ── Tx B: land the result, or delete the placeholder on failure. ──────────
    async with async_session_factory() as session:
        placeholder = await session.get(CountryRule, rule_id)
        if placeholder is None:  # deleted concurrently; nothing to do
            raise AppError("Generation record disappeared", code="generation_lost")

        if draft is None:
            await session.delete(placeholder)
            await session.commit()
            raise AppError("AI drafting failed or is not configured", code="ai_generation_failed")

        placeholder.summary = draft.summary
        placeholder.retrieved_url = draft.retrieved_url
        placeholder.grounded = draft.grounded
        placeholder.generated_by_model = draft.model
        # Pre-fill the admin-verified source only when grounded (§12.2).
        placeholder.source_url = draft.retrieved_url if draft.grounded else None
        placeholder.requirements = [
            CountryRuleRequirement(text=t, display_order=i)
            for i, t in enumerate(draft.requirements)
        ]
        placeholder.pitfalls = [
            CountryRulePitfall(text=t, display_order=i) for i, t in enumerate(draft.pitfalls)
        ]
        placeholder.process_notes = [
            CountryRuleProcessNote(text=t, display_order=i)
            for i, t in enumerate(draft.process_notes)
        ]
        placeholder.status = RulePublishStatus.draft
        placeholder.updated_by = admin_id
        session.add(placeholder)
        await session.commit()

    return rule_id


# ── Read helpers (use the request session; route paginates) ──────────────────


def list_rules_stmt(
    *,
    country_code: str | None = None,
    visa_type: str | None = None,
    status: RulePublishStatus | None = None,
    sort: str | None = None,
) -> Select[tuple[CountryRule]]:
    stmt = select(CountryRule).where(CountryRule.is_archived.is_(False))
    if country_code:
        stmt = stmt.where(CountryRule.country_code == country_code.upper())
    if visa_type:
        parsed = parse_visa_type(visa_type)
        stmt = stmt.where(CountryRule.visa_type == (parsed.value if parsed else visa_type))
    if status is not None:
        stmt = stmt.where(CountryRule.status == status)
    if sort == "newest":
        return stmt.order_by(CountryRule.created_at.desc(), CountryRule.id.desc())
    return stmt.order_by(
        CountryRule.country_code,
        CountryRule.visa_type,
        CountryRule.version.desc(),
    )


async def get_rule(session: AsyncSession, rule_id: uuid.UUID) -> CountryRule:
    rule = await session.get(CountryRule, rule_id)
    if rule is None or rule.is_archived:
        raise NotFoundError("Country rule not found")
    return rule


async def get_published(
    session: AsyncSession, country_code: str, visa_type: str
) -> CountryRule | None:
    """The single published policy for a pair, or None. Used by assessment."""
    result = await session.execute(
        select(CountryRule).where(
            CountryRule.country_code == country_code.upper(),
            CountryRule.visa_type == visa_type.lower(),
            CountryRule.status == RulePublishStatus.published,
        )
    )
    return result.scalars().first()


# ── Mutations (use the request session; commit handled by SessionDep) ────────


async def update_draft(
    session: AsyncSession,
    rule: CountryRule,
    *,
    summary: str | None = None,
    requirements: list[str] | None = None,
    pitfalls: list[str] | None = None,
    process_notes: list[str] | None = None,
    source_url: str | None = None,
    admin_id: uuid.UUID,
) -> CountryRule:
    """Edit a draft. Published/archived rules are immutable (§18)."""
    if rule.status != RulePublishStatus.draft:
        raise ConflictError("Only draft policies can be edited; generate a new version instead")

    if summary is not None:
        rule.summary = summary
    if source_url is not None:
        rule.source_url = source_url or None
    if requirements is not None:
        rule.requirements = [
            CountryRuleRequirement(text=t, display_order=i) for i, t in enumerate(requirements)
        ]
    if pitfalls is not None:
        rule.pitfalls = [
            CountryRulePitfall(text=t, display_order=i) for i, t in enumerate(pitfalls)
        ]
    if process_notes is not None:
        rule.process_notes = [
            CountryRuleProcessNote(text=t, display_order=i) for i, t in enumerate(process_notes)
        ]
    rule.updated_by = admin_id
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def publish(session: AsyncSession, rule: CountryRule, admin_id: uuid.UUID) -> CountryRule:
    """Publish a verified draft, archiving the currently published version (§19).

    Runs in the request transaction: archive-then-publish is atomic, and the
    partial unique index is the final backstop against two published versions.
    """
    if rule.status != RulePublishStatus.draft:
        raise ConflictError("Only draft policies can be published")
    if not rule.source_url:
        raise AppError("A source URL is required before publishing", code="missing_source_url")
    if not rule.requirements:
        raise AppError(
            "At least one requirement is required before publishing", code="no_requirements"
        )

    # Archive the current published version for this pair, if any.
    current = await get_published(session, rule.country_code, rule.visa_type)
    if current is not None and current.id != rule.id:
        current.status = RulePublishStatus.archived
        current.updated_by = admin_id
        session.add(current)
        await session.flush()

    rule.status = RulePublishStatus.published
    rule.published_at = datetime.now(UTC)
    rule.updated_by = admin_id
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def archive(session: AsyncSession, rule: CountryRule, admin_id: uuid.UUID) -> CountryRule:
    """Admin-archive a policy so it is NOT used by AI assessment.

    Works on ``draft`` (park it without deleting) or ``published`` (retire the
    live policy). Archiving a published rule leaves the pair with no published
    policy — assessment falls back to the base prompt for that country/visa.
    Generating rows cannot be archived (a generation is still in flight).
    """
    if rule.status == RulePublishStatus.generating:
        raise ConflictError("A generating policy cannot be archived")
    if rule.status == RulePublishStatus.archived:
        return rule  # idempotent
    rule.status = RulePublishStatus.archived
    rule.published_at = None
    rule.updated_by = admin_id
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def restore(session: AsyncSession, rule: CountryRule, admin_id: uuid.UUID) -> CountryRule:
    """Restore an archived policy back to ``draft`` so it can be edited/published.

    It returns to ``draft`` (never straight to ``published``) so the admin must
    re-publish it to make it live again — that keeps the "human verifies before
    it reaches assessment" trust boundary intact and avoids ever creating a
    second published version by surprise.
    """
    if rule.status != RulePublishStatus.archived:
        raise ConflictError("Only archived policies can be restored")
    rule.status = RulePublishStatus.draft
    rule.updated_by = admin_id
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def delete_rule(session: AsyncSession, rule: CountryRule) -> None:
    """Delete a draft or archived rule. Published rules cannot be deleted."""
    if rule.status == RulePublishStatus.published:
        raise ConflictError("Published policies cannot be deleted; publish a new version instead")
    await session.delete(rule)
    await session.flush()
