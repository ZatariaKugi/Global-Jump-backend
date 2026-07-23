"""Normalize advisor bookable/offered services to ``AdvisorServiceType`` enum values.

Fixes staging/local advisors that were seeded with freeform labels like
``consultation`` / ``consultation_30``, which cause::

    validation_error on POST /bookings body.service_type

Targets advisor ``9241f0ec-8ede-4d06-9e31-59e951877843`` by default; pass
``--all`` to remap every invalid row across the DB.

Run with::

    uv run python -m scripts.fix_advisor_service_enums
    uv run python -m scripts.fix_advisor_service_enums --all
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import async_session_factory, engine
from app.models.advisor_profile import (
    AdvisorOfferedService,
    AdvisorProfile,
    AdvisorService,
    AdvisorServiceType,
)

logger = get_logger(__name__)

DEFAULT_ADVISOR_ID = uuid.UUID("9241f0ec-8ede-4d06-9e31-59e951877843")
VALID = {e.value for e in AdvisorServiceType}

# Common freeform / legacy labels → enum.
ALIASES: dict[str, str] = {
    "consultation": AdvisorServiceType.immigration_specialist.value,
    "consultation_30": AdvisorServiceType.immigration_specialist.value,
    "consultation_60": AdvisorServiceType.immigration_specialist.value,
    "consultation_90": AdvisorServiceType.career_coach.value,
    "document_review": AdvisorServiceType.immigration_specialist.value,
    "immigration": AdvisorServiceType.immigration_specialist.value,
    "immigration_consulting": AdvisorServiceType.immigration_specialist.value,
    "career": AdvisorServiceType.career_coach.value,
    "coaching": AdvisorServiceType.career_coach.value,
    "resume": AdvisorServiceType.resume_writer.value,
    "cv": AdvisorServiceType.resume_writer.value,
    "recruiting": AdvisorServiceType.recruiter.value,
    "recruitment": AdvisorServiceType.recruiter.value,
    "pets": AdvisorServiceType.pet_transport.value,
    "pet": AdvisorServiceType.pet_transport.value,
    "moving": AdvisorServiceType.household_goods_transport.value,
    "household": AdvisorServiceType.household_goods_transport.value,
    "real_estate": AdvisorServiceType.realtor.value,
    "realty": AdvisorServiceType.realtor.value,
}

DEFAULT_SERVICES: list[tuple[str, int, float]] = [
    (AdvisorServiceType.immigration_specialist.value, 30, 99.0),
    (AdvisorServiceType.career_coach.value, 60, 149.0),
]


def _normalize(raw: str) -> str | None:
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if key in VALID:
        return key
    if key in ALIASES:
        return ALIASES[key]
    # fuzzy contains
    for alias, mapped in ALIASES.items():
        if alias in key or key in alias:
            return mapped
    return None


async def _fix_profile(session, profile: AdvisorProfile) -> list[str]:
    lines: list[str] = []
    changed = False

    for svc in list(profile.services or []):
        mapped = _normalize(svc.service_type)
        if mapped is None:
            lines.append(f"  drop invalid service={svc.service_type!r}")
            await session.delete(svc)
            changed = True
        elif mapped != svc.service_type:
            lines.append(f"  service {svc.service_type!r} → {mapped!r}")
            svc.service_type = mapped
            changed = True

    for offered in list(profile.offered_services or []):
        mapped = _normalize(offered.service_type)
        if mapped is None:
            lines.append(f"  drop invalid offered={offered.service_type!r}")
            await session.delete(offered)
            changed = True
        elif mapped != offered.service_type:
            lines.append(f"  offered {offered.service_type!r} → {mapped!r}")
            offered.service_type = mapped
            changed = True

    await session.flush()
    await session.refresh(profile)

    # Deduplicate services by type (keep cheapest / first).
    seen: set[str] = set()
    for svc in list(profile.services or []):
        if svc.service_type in seen:
            lines.append(f"  drop duplicate service={svc.service_type}")
            await session.delete(svc)
            changed = True
        else:
            seen.add(svc.service_type)

    seen_offered: set[str] = set()
    for offered in list(profile.offered_services or []):
        if offered.service_type in seen_offered:
            lines.append(f"  drop duplicate offered={offered.service_type}")
            await session.delete(offered)
            changed = True
        else:
            seen_offered.add(offered.service_type)

    await session.flush()
    await session.refresh(profile)

    if not profile.services:
        for st, dur, price in DEFAULT_SERVICES:
            session.add(
                AdvisorService(
                    profile_id=profile.id,
                    service_type=st,
                    duration_minutes=dur,
                    price_usd=price,
                )
            )
            lines.append(f"  seeded service={st} ${price}/{dur}m")
            changed = True
        for st, _dur, _price in DEFAULT_SERVICES:
            session.add(AdvisorOfferedService(profile_id=profile.id, service_type=st))
        await session.flush()

    if not changed and not lines:
        lines.append("  ok (already enum-valid)")
    return lines


async def run(*, advisor_id: uuid.UUID | None, fix_all: bool) -> list[str]:
    out: list[str] = []
    async with async_session_factory() as session:
        if fix_all:
            profiles = (await session.execute(select(AdvisorProfile))).scalars().all()
        else:
            assert advisor_id is not None
            profile = await session.scalar(
                select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_id)
            )
            if profile is None:
                return [f"error=no_profile_for_user {advisor_id}"]
            profiles = [profile]

        for profile in profiles:
            out.append(f"advisor_user_id={profile.user_id}")
            out.extend(await _fix_profile(session, profile))

        await session.commit()
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--advisor-id",
        default=str(DEFAULT_ADVISOR_ID),
        help="Advisor user UUID to fix (ignored with --all)",
    )
    parser.add_argument("--all", action="store_true", help="Fix every advisor profile")
    args = parser.parse_args()
    try:
        for line in await run(
            advisor_id=None if args.all else uuid.UUID(args.advisor_id),
            fix_all=args.all,
        ):
            print(line)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
