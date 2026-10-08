"""What a user may do under their plan (EPIC 04, PAY-110 / PAY-111; free plan 2026-10-08).

Resolution order: the user's live subscription (its *snapshotted* perks, so an admin
edit of the plan never reaches a live subscriber mid-period), else the audience's
designated default free plan, else nothing — and nothing means **locked**, not open.
The one exception is a user with no audience at all (admins), who is never gated.

Number-type features are metered per period in ``subscription_usage``: the Stripe
billing period for subscribers, the UTC calendar month otherwise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.pricing_plan import FeatureKind, PricingPlan
from app.models.subscription import Subscription, SubscriptionStatus, SubscriptionUsage
from app.models.user import User
from app.schemas.subscription import EntitlementsRead, FeatureEntitlementRead
from app.services import payment_config_service, pricing_plan_service

LIVE_STATUSES = (SubscriptionStatus.active, SubscriptionStatus.trialing)
GRACE_STATUSES = (
    SubscriptionStatus.past_due,
    SubscriptionStatus.canceled,
    SubscriptionStatus.unpaid,
)
AUDIENCES = ("seeker", "advisor")


@dataclass(frozen=True, slots=True)
class Period:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class Perk:
    """One enforceable feature as it applies to a user, whichever table it came from."""

    key: str
    label: str
    value_type: str  # number | bool
    value: str
    is_enabled: bool


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def calendar_month(now: datetime) -> Period:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return Period(start=start, end=nxt)


def paid_through(sub: Subscription, grace_days: int) -> datetime | None:
    """When a live subscription stops granting its plan, or ``None`` if unknowable.

    ``access_until`` is deliberately not consulted here: it is written by two paths
    that disagree (``on_invoice_paid`` adds the grace, ``_apply_stripe_subscription``
    does not), and it exists to extend access to a subscription that has *ended* --
    which is the ``GRACE_STATUSES`` branch below, not this one.

    ``None`` means we hold no period end, so there is nothing to judge the row by.
    Access is never removed on a guess.
    """
    end = _utc(sub.current_period_end)
    return None if end is None else end + timedelta(days=grace_days)


def has_lapsed(sub: Subscription, grace_days: int, now: datetime) -> bool:
    """A row that still says ``active`` while the period it paid for is over.

    Stripe's ending event is the only thing that would have corrected the status,
    and it is the event this project has watched fail for a week at a time. So the
    read path has to be able to tell -- nothing else ever will.
    """
    boundary = paid_through(sub, grace_days)
    return boundary is not None and boundary <= now


async def grace_days(session: AsyncSession) -> int:
    config = await payment_config_service.get_config(session)
    return int(getattr(config, "subscription_grace_days", 3))


async def current_subscription(session: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    """The subscription that currently grants access, if any.

    #145 [PAY-110] and #146 [PAY-111] both require that an expired or lapsed
    subscription revokes access, so a live-looking row only counts while the period
    it paid for (plus the configured grace) still holds.
    """
    now = datetime.now(UTC)
    days = await grace_days(session)
    rows = list(
        (
            await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .order_by(Subscription.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for sub in rows:
        if sub.status in LIVE_STATUSES:
            if not has_lapsed(sub, days, now):
                return sub
            continue
        until = _utc(sub.access_until)
        if sub.status in GRACE_STATUSES and until is not None and until > now:
            return sub
    return None


def audience_of(user: User) -> str | None:
    role = user.role.value
    return role if role in AUDIENCES else None


async def resolve_plan(
    session: AsyncSession, user: User
) -> tuple[PricingPlan | None, Subscription | None]:
    sub = await current_subscription(session, user.id)
    if sub is not None:
        plan = await session.get(PricingPlan, sub.plan_id)
        if plan is not None:
            return plan, sub
    audience = audience_of(user)
    if audience is None:
        return None, None
    return await pricing_plan_service.free_plan_for(session, audience), None


def plan_perks(plan: PricingPlan) -> list[Perk]:
    out: list[Perk] = []
    for f in plan.features:
        key = pricing_plan_service.canonical_key(f.feature_key)
        if f.kind != FeatureKind.enforceable or not key:
            continue
        out.append(
            Perk(
                key=key,
                label=f.label,
                value_type=str(getattr(f.value_type, "value", f.value_type)),
                value=f.value,
                is_enabled=f.is_enabled,
            )
        )
    return out


def perks_for(plan: PricingPlan, sub: Subscription | None) -> list[Perk]:
    """The perks that apply: the subscription's snapshot when it has one (BUG21),
    else the plan's current rows (free-plan users, and rows written before snapshots)."""
    if sub is not None and sub.features:
        return [
            Perk(
                key=pricing_plan_service.canonical_key(f.feature_key) or f.feature_key,
                label=f.label,
                value_type=f.value_type,
                value=f.value,
                is_enabled=f.is_enabled,
            )
            for f in sub.features
        ]
    return plan_perks(plan)


def period_for(sub: Subscription | None, now: datetime) -> Period:
    if sub is not None and sub.current_period_start and sub.current_period_end:
        return Period(start=_utc(sub.current_period_start), end=_utc(sub.current_period_end))  # type: ignore[arg-type]
    return calendar_month(now)


async def _usage_row(
    session: AsyncSession, user_id: uuid.UUID, key: str, period: Period, *, create: bool
) -> SubscriptionUsage | None:
    row = (
        await session.execute(
            select(SubscriptionUsage).where(
                SubscriptionUsage.user_id == user_id,
                SubscriptionUsage.feature_key == key,
                SubscriptionUsage.period_start == period.start,
            )
        )
    ).scalar_one_or_none()
    if row is None and create:
        row = SubscriptionUsage(
            user_id=user_id,
            feature_key=key,
            period_start=period.start,
            period_end=period.end,
            used=0,
        )
        session.add(row)
        await session.flush()
    return row


def _limit_of(feature_value: str) -> int | None:
    v = (feature_value or "").strip().lower()
    if v in ("", "unlimited"):
        return None
    return int(v)


def _locked(user: User) -> EntitlementsRead:
    """No plan applies. An admin is unrestricted; a seeker or advisor is locked."""
    return EntitlementsRead(
        plan_id=None,
        plan_name=None,
        subscription_status=None,
        access_until=None,
        unrestricted=audience_of(user) is None,
        features={},
    )


async def get_entitlements(session: AsyncSession, user: User) -> EntitlementsRead:
    plan, sub = await resolve_plan(session, user)
    now = datetime.now(UTC)
    if plan is None:
        return _locked(user)
    period = period_for(sub, now)
    features: dict[str, FeatureEntitlementRead] = {}
    for perk in perks_for(plan, sub):
        enabled = perk.is_enabled
        if perk.value_type == "bool" and enabled:
            enabled = (perk.value or "true").strip().lower() != "false"
        limit: int | None = None
        used = 0
        remaining: int | None = None
        resets_at: datetime | None = None
        if perk.value_type == "number":
            limit = _limit_of(perk.value)
            row = await _usage_row(session, user.id, perk.key, period, create=False)
            used = row.used if row else 0
            remaining = None if limit is None else max(0, limit - used)
            resets_at = period.end
        features[perk.key] = FeatureEntitlementRead(
            key=perk.key,
            label=perk.label,
            enabled=enabled,
            value_type=perk.value_type,
            value=perk.value,
            limit=limit,
            used=used,
            remaining=remaining,
            resets_at=resets_at,
        )
    return EntitlementsRead(
        plan_id=plan.id,
        plan_name=plan.name,
        subscription_status=sub.status.value if sub else None,
        access_until=_utc(sub.access_until) if sub else None,
        unrestricted=False,
        features=features,
    )


def _refuse() -> AppError:
    return AppError(
        "This feature is not included in your current plan", code="subscription_required"
    )


async def check(session: AsyncSession, user: User, key: str) -> None:
    """Raise when the user's plan does not allow ``key`` right now."""
    ent = await get_entitlements(session, user)
    if ent.unrestricted:
        return
    feature = ent.features.get(key)
    if feature is None or not feature.enabled:
        raise _refuse()
    if feature.limit is not None and feature.used >= feature.limit:
        raise AppError(
            f"You have used all {feature.limit} of your plan's {feature.label.lower()} "
            "for this period",
            code="quota_exceeded",
        )


async def allowed(session: AsyncSession, user: User, key: str) -> bool:
    """``check`` as a boolean (for endpoints that degrade instead of refusing)."""
    try:
        await check(session, user, key)
    except AppError:
        return False
    return True


async def granted(session: AsyncSession, user: User, key: str) -> bool:
    """True only when a plan explicitly grants ``key``; never for the unrestricted case."""
    ent = await get_entitlements(session, user)
    if ent.unrestricted:
        return False
    feature = ent.features.get(key)
    return feature is not None and feature.enabled


async def can_use(session: AsyncSession, user_id: uuid.UUID, key: str) -> bool:
    """``allowed`` for a user we only hold the id of (matching, lead generation).

    Unlike ``granted`` this is the user's own view: an admin is unrestricted, a
    perk switched on with a spent quota is not usable.
    """
    user = await session.get(User, user_id)
    if user is None:
        return False
    return await allowed(session, user, key)


async def limit_for(session: AsyncSession, user: User, key: str) -> int | None:
    """Plan cap for a count-type feature; ``None`` means uncapped.

    Raises ``subscription_required`` when the applying plan leaves the feature out
    or off. With no plan at all a seeker or advisor gets a cap of zero.
    """
    ent = await get_entitlements(session, user)
    if ent.unrestricted:
        return None
    if ent.plan_id is None:
        return 0
    feature = ent.features.get(key)
    if feature is None or not feature.enabled:
        raise _refuse()
    return feature.limit


async def assert_within(session: AsyncSession, user: User, key: str, current: int) -> None:
    """Gate for features that cap how many of something the user holds at once
    (bookmarks, documents): ``current`` is what they hold now."""
    limit = await limit_for(session, user, key)
    if limit is not None and current >= limit:
        raise AppError(
            f"Your plan allows {limit} {key.replace('_', ' ')}. Upgrade to add more.",
            code="quota_exceeded",
        )


async def consume(session: AsyncSession, user: User, key: str) -> None:
    """``check`` then count one use for a number-type feature."""
    await check(session, user, key)
    plan, sub = await resolve_plan(session, user)
    if plan is None:
        return
    perk = next((p for p in perks_for(plan, sub) if p.key == key), None)
    if perk is None or perk.value_type != "number":
        return
    period = period_for(sub, datetime.now(UTC))
    row = await _usage_row(session, user.id, key, period, create=True)
    assert row is not None
    row.used += 1
    session.add(row)
    await session.flush()
