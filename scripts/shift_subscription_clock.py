"""Move a subscription's billing period so expiry behaviour can be tested today.

Subscriptions expire once a month, so testing what happens at the boundary would
otherwise mean waiting for it. This shifts the stored period instead: the clock
moves, nothing else does. It writes only ``current_period_start`` and
``current_period_end`` — never ``status``, never ``access_until``, never a flag.
The application still decides for itself what that period means, which is the
whole point: the decision is what is under test.

Run with:
    uv run python -m scripts.shift_subscription_clock --list
    uv run python -m scripts.shift_subscription_clock a@b.com --days -10
    uv run python -m scripts.shift_subscription_clock a@b.com --days 20

``--days`` is where ``current_period_end`` lands relative to now: negative for a
period that has already finished, positive to put it back in the future. The start
is kept one interval behind the end.

Refuses to run outside a local environment. Development only — it is not imported
by the application and has no effect on it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import async_session_factory, engine
from app.models.pricing_plan import PricingPlan
from app.models.subscription import Subscription
from app.models.user import User

PERIOD_DAYS = 30


def _guard_environment() -> None:
    settings = get_settings()
    environment = settings.ENVIRONMENT.value
    if environment not in ("local", "development", "test"):
        sys.exit(
            f"Refusing to run: ENVIRONMENT is {environment!r}. "
            "This script only ever runs against a local database."
        )


def _aware(value: datetime | None) -> datetime | None:
    """Stored datetimes can come back naive; compare them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _describe(sub: Subscription, email: str, plan_name: str, grace_days: int) -> str:
    end = _aware(sub.current_period_end)
    now = datetime.now(UTC)
    if end is None:
        state = "no period end stored"
    elif end + timedelta(days=grace_days) <= now:
        state = f"LAPSED (grace {grace_days}d ran out {(now - end).days - grace_days}d ago)"
    elif end <= now:
        state = f"inside its {grace_days}-day grace"
    else:
        state = f"live, {(end - now).days}d left"
    return (
        f"  {email:<34} {plan_name:<12} {sub.status.value:<10} "
        f"ends {end:%Y-%m-%d %H:%M} UTC   {state}"
    )


async def _grace_days() -> int:
    from app.services import payment_config_service

    async with async_session_factory() as session:
        config = await payment_config_service.get_config(session)
        return int(getattr(config, "subscription_grace_days", 3))


async def list_subscriptions() -> None:
    grace_days = await _grace_days()
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Subscription, User, PricingPlan)
                .join(User, User.id == Subscription.user_id)
                .join(PricingPlan, PricingPlan.id == Subscription.plan_id)
                .order_by(Subscription.created_at.desc())
            )
        ).all()
    if not rows:
        print("No subscriptions in this database.")
        return
    print(f"\n{len(rows)} subscription(s), grace period {grace_days} days:\n")
    for sub, user, plan in rows:
        print(_describe(sub, user.email, plan.name, grace_days))
    print()


async def shift(email: str, days: int) -> None:
    grace_days = await _grace_days()
    now = datetime.now(UTC)
    new_end = now + timedelta(days=days)
    new_start = new_end - timedelta(days=PERIOD_DAYS)

    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            sys.exit(f"No user with email {email!r}.")
        sub = (
            await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if sub is None:
            sys.exit(f"{email} has no subscription to shift.")
        plan = await session.get(PricingPlan, sub.plan_id)
        plan_name = plan.name if plan else "?"

        print("\nbefore:")
        print(_describe(sub, email, plan_name, grace_days))

        sub.current_period_start = new_start
        sub.current_period_end = new_end
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        print("after:")
        print(_describe(sub, email, plan_name, grace_days))

    if days < 0:
        print(
            "\nThe subscription now looks lapsed to the application. Stripe still has "
            "its own opinion:\n"
            "  * entitlements, the admin badge and the checkout guard decide locally, "
            "so they change straight away;\n"
            "  * the subscriber's own page asks Stripe first, and will import the "
            "subscription back if Stripe still says it is live.\n"
            "To test the missed-webhook case end to end, cancel it in Stripe (or stop "
            "the tunnel) as well.\n"
        )
    else:
        print("\nThe subscription is live again.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move a subscription's billing period for local testing."
    )
    parser.add_argument("email", nargs="?", help="the subscriber's email address")
    parser.add_argument(
        "--days",
        type=int,
        help="days from now for current_period_end; negative for an ended period",
    )
    parser.add_argument(
        "--list", action="store_true", help="show every subscription and its state"
    )
    args = parser.parse_args()

    _guard_environment()

    if args.list:
        asyncio.run(_run(list_subscriptions()))
        return
    if not args.email or args.days is None:
        parser.error("give an email and --days, or use --list")
    asyncio.run(_run(shift(args.email, args.days)))


async def _run(coro) -> None:  # type: ignore[no-untyped-def]
    try:
        await coro
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
