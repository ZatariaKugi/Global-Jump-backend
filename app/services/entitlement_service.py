"""What a user may do under their plan (EPIC 04, PAY-110 / PAY-111).

Resolution order: the user's live subscription's plan, else the audience's free
plan (an active zero-price plan), else *no plan* which means unrestricted, so an
environment where the admin has not configured plans yet never locks anyone out.

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
from app.models.pricing_plan import FeatureKind, FeatureValueType, PricingPlan
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


@dataclass(frozen=True, slots=True)
class Period:
    start: datetime
    end: datetime


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


async def resolve_plan(
    session: AsyncSession, user: User
) -> tuple[PricingPlan | None, Subscription | None]:
    sub = await current_subscription(session, user.id)
    if sub is not None:
        plan = await session.get(PricingPlan, sub.plan_id)
        if plan is not None:
            return plan, sub
    audience = user.role.value if user.role.value in ("seeker", "advisor") else None
    if audience is None:
        return None, None
    return await pricing_plan_service.free_plan_for(session, audience), None


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


async def get_entitlements(session: AsyncSession, user: User) -> EntitlementsRead:
    plan, sub = await resolve_plan(session, user)
    now = datetime.now(UTC)
    if plan is None:
        return EntitlementsRead(
            plan_id=None,
            plan_name=None,
            subscription_status=None,
            access_until=None,
            unrestricted=True,
            features={},
        )
    period = period_for(sub, now)
    features: dict[str, FeatureEntitlementRead] = {}
    for f in plan.features:
        if f.kind != FeatureKind.enforceable or not f.feature_key:
            continue
        enabled = f.is_enabled
        if f.value_type == FeatureValueType.bool and enabled:
            enabled = (f.value or "true").strip().lower() != "false"
        limit: int | None = None
        used = 0
        remaining: int | None = None
        resets_at: datetime | None = None
        if f.value_type == FeatureValueType.number:
            limit = _limit_of(f.value)
            row = await _usage_row(session, user.id, f.feature_key, period, create=False)
            used = row.used if row else 0
            remaining = None if limit is None else max(0, limit - used)
            resets_at = period.end
        features[f.feature_key] = FeatureEntitlementRead(
            key=f.feature_key,
            label=f.label,
            enabled=enabled,
            value_type=f.value_type.value,
            value=f.value,
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


async def check(session: AsyncSession, user: User, key: str) -> None:
    """Raise when the user's plan does not allow ``key`` right now."""
    ent = await get_entitlements(session, user)
    if ent.unrestricted:
        return
    feature = ent.features.get(key)
    if feature is None or not feature.enabled:
        raise AppError(
            "This feature is not included in your current plan", code="subscription_required"
        )
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
    """True only when a plan explicitly grants ``key``.

    For perks (priority support, featured listing) the unrestricted case means
    "nothing configured", not "everyone gets the perk".
    """
    ent = await get_entitlements(session, user)
    if ent.unrestricted:
        return False
    feature = ent.features.get(key)
    return feature is not None and feature.enabled


async def limit_for(session: AsyncSession, user: User, key: str) -> int | None:
    """Plan cap for a count-type feature; ``None`` means uncapped.

    Raises ``subscription_required`` when a plan applies and the feature is off.
    """
    ent = await get_entitlements(session, user)
    if ent.unrestricted:
        return None
    feature = ent.features.get(key)
    if feature is None or not feature.enabled:
        raise AppError(
            "This feature is not included in your current plan", code="subscription_required"
        )
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
    feature = next(
        (f for f in plan.features if f.kind == FeatureKind.enforceable and f.feature_key == key),
        None,
    )
    if feature is None or feature.value_type != FeatureValueType.number:
        return
    period = period_for(sub, datetime.now(UTC))
    row = await _usage_row(session, user.id, key, period, create=True)
    assert row is not None
    row.used += 1
    session.add(row)
    await session.flush()
