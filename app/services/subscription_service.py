"""Stripe subscriptions for the Advisor Subscription and the Seeker AI Assessment
Unlock (EPIC 04, PAY-110 / PAY-111).

State changes only through webhooks; the API side creates Checkout Sessions,
Billing Portal sessions and cancel / change requests. Every outbound write carries an
idempotency key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import stripe
import structlog
from sqlalchemy import Select, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError
from app.models.advisor_profile import AdvisorProfile
from app.models.notification import NotificationEntityType, NotificationType
from app.models.pricing_plan import PlanStatus, PricingPlan
from app.models.subscription import Subscription, SubscriptionInvoice, SubscriptionStatus
from app.models.user import User, UserRole
from app.schemas.payment import CheckoutResponse
from app.schemas.subscription import (
    AdminInvoiceRead,
    AdminSubscriptionPlanRevenueRead,
    AdminSubscriptionRead,
    AdminSubscriptionSummaryRead,
    ChangePlanPreviewRead,
    InvoiceLineRead,
    PaymentMethodSummary,
    SubscriptionInvoiceRead,
    SubscriptionPlanBrief,
    SubscriptionRead,
)
from app.services import (
    email_service,
    notification_service,
    payment_config_service,
    stripe_config_service,
)

log = structlog.get_logger()

_ACTIVE_LIKE = (
    SubscriptionStatus.incomplete,
    SubscriptionStatus.trialing,
    SubscriptionStatus.active,
    SubscriptionStatus.past_due,
)
_ENDED = (SubscriptionStatus.canceled, SubscriptionStatus.unpaid, SubscriptionStatus.expired)
# Stripe's own status values that mean "this customer is already paying for a plan".
_LIVE_STRIPE_STATUSES = ("trialing", "active", "past_due")


async def _free_plan_grants(session: AsyncSession, audience: str, key: str) -> bool:
    """Whether the audience's free plan keeps ``key`` on once a paid plan ends.

    No free plan configured means nothing is restricted, so that counts as granted.
    """
    from app.models.pricing_plan import FeatureKind, FeatureValueType
    from app.services import pricing_plan_service

    free = await pricing_plan_service.free_plan_for(session, audience)
    if free is None:
        return True
    for f in free.features:
        if f.kind != FeatureKind.enforceable or f.feature_key != key or not f.is_enabled:
            continue
        if f.value_type == FeatureValueType.bool:
            return (f.value or "true").strip().lower() != "false"
        return True
    return False


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (KeyError, TypeError, AttributeError, IndexError):
        return getattr(obj, key, default)


def _ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), tz=UTC)


async def _init_stripe(session: AsyncSession, settings: Settings) -> None:
    """Point the SDK at the credentials in force: admin-saved, else the environment."""
    keys = await stripe_config_service.effective_keys(session, settings)
    if not keys.secret_key:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    stripe.api_key = keys.secret_key


def _map_status(raw: str | None) -> SubscriptionStatus:
    mapping = {
        "incomplete": SubscriptionStatus.incomplete,
        "incomplete_expired": SubscriptionStatus.expired,
        "trialing": SubscriptionStatus.trialing,
        "active": SubscriptionStatus.active,
        "past_due": SubscriptionStatus.past_due,
        "canceled": SubscriptionStatus.canceled,
        "unpaid": SubscriptionStatus.unpaid,
        "paused": SubscriptionStatus.past_due,
    }
    return mapping.get(str(raw or ""), SubscriptionStatus.incomplete)


def _return_base(settings: Settings, role: str) -> str:
    return f"{settings.FRONTEND_URL}/{role}/subscription/return"


# ── customer + checkout ──────────────────────────────────────────────────────


async def ensure_customer(session: AsyncSession, user: User, settings: Settings) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id
    await _init_stripe(session, settings)
    customer = await stripe.Customer.create_async(
        email=user.email,
        name=user.full_name or user.email,
        metadata={"user_id": str(user.id), "role": user.role.value},
        idempotency_key=f"customer_{user.id}",
    )
    user.stripe_customer_id = str(customer.id)
    session.add(user)
    await session.flush()
    return user.stripe_customer_id


async def _plan_for_purchase(session: AsyncSession, user: User, plan_id: uuid.UUID) -> PricingPlan:
    plan = await session.get(PricingPlan, plan_id)
    if plan is None or plan.status != PlanStatus.active:
        raise AppError("This plan is not available", code="plan_not_available")
    if plan.audience.value != user.role.value:
        raise AppError("This plan is for a different account type", code="wrong_audience")
    return plan


async def _active_subscription(session: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    """The row that counts as "this user is already subscribed".

    A row still marked ``active`` whose paid period ended is not that. Treating it as
    live was costing twice over: the plan kept being granted for nothing, and
    ``create_checkout`` refused the user a new subscription with ``already_subscribed``
    -- so they could not pay even when they wanted to.
    """
    from app.services import entitlement_service

    rows = list(
        (
            await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user_id, Subscription.status.in_(_ACTIVE_LIKE))
                .order_by(Subscription.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    days = await entitlement_service.grace_days(session)
    now = datetime.now(UTC)
    for sub in rows:
        if not entitlement_service.has_lapsed(sub, days, now):
            return sub
    return None


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _live_stripe_subscription(
    session: AsyncSession, user: User, customer_id: str, settings: Settings
) -> tuple[bool, bool, Subscription | None]:
    """Ask Stripe — not our table — what this customer actually pays for.

    ``subscriptions`` only learns about a subscription when the webhook arrives, so
    between a completed checkout and that delivery — or for as long as delivery is
    broken — our table says "free plan" while the customer is being billed. Stripe is
    the source of truth for this question.

    Returns ``(reachable, found, row)``. ``reachable`` separates "Stripe answered and
    this customer pays for nothing" from "we could not ask", which look identical from
    here and must not: only the first is grounds for ending a subscription locally.
    ``row`` is the local row imported for a live subscription — through the ordinary
    webhook path, so one code path owns the mapping — and is ``None`` when the Stripe
    metadata does not map onto a user and plan we hold.
    """
    await _init_stripe(session, settings)
    try:
        listed = await stripe.Subscription.list_async(customer=customer_id, status="all", limit=20)
    except stripe.StripeError as exc:
        # A probe failure must never block a checkout or break a read.
        log.warning("subscription_probe_failed", user_id=str(user.id), error=str(exc)[:200])
        return False, False, None

    for stripe_sub in _get(listed, "data") or []:
        if str(_get(stripe_sub, "status") or "") not in _LIVE_STRIPE_STATUSES:
            continue
        metadata = _get(stripe_sub, "metadata") or {}
        plan = None
        if str(_get(metadata, "user_id") or "") == str(user.id):
            plan_uuid = _as_uuid(_get(metadata, "plan_id"))
            plan = await session.get(PricingPlan, plan_uuid) if plan_uuid else None
        if plan is None:
            # Unmappable metadata is still someone's money — report it without importing.
            log.warning(
                "subscription_probe_unmappable",
                user_id=str(user.id),
                stripe_subscription_id=str(_get(stripe_sub, "id") or ""),
            )
            return True, True, None
        await on_subscription_event(session, stripe_sub, settings)
        return True, True, await _find_by_stripe_id(session, str(_get(stripe_sub, "id") or ""))
    return True, False, None


async def _stripe_already_subscribed(
    session: AsyncSession, user: User, customer_id: str, settings: Settings
) -> bool:
    _reachable, found, _row = await _live_stripe_subscription(session, user, customer_id, settings)
    return found


async def create_checkout(
    session: AsyncSession, user: User, plan_id: uuid.UUID, settings: Settings
) -> CheckoutResponse:
    plan = await _plan_for_purchase(session, user, plan_id)
    if Decimal(str(plan.price_usd)) <= 0:
        raise AppError("The free plan needs no checkout", code="plan_is_free")
    if not plan.stripe_price_id:
        raise AppError("This plan is not available", code="plan_not_available")
    existing = await _active_subscription(session, user.id)
    if existing is not None and existing.status != SubscriptionStatus.incomplete:
        raise AppError(
            "You already have a subscription; change your plan instead", code="already_subscribed"
        )
    config = await payment_config_service.get_config(session)
    keys = await stripe_config_service.effective_keys(session, settings)
    stripe_config_service.assert_live_allowed(keys, config)
    had_customer = bool(user.stripe_customer_id)
    try:
        customer_id = await ensure_customer(session, user, settings)
    except stripe.StripeError as exc:
        raise stripe_config_service.checkout_failed(exc, user_id=str(user.id)) from exc
    # A customer created in this very call cannot already own a subscription, so the
    # probe is skipped for first-time subscribers (PAY-106).
    if had_customer and await _stripe_already_subscribed(session, user, customer_id, settings):
        raise AppError(
            "You already have a subscription; change your plan instead", code="already_subscribed"
        )
    base = _return_base(settings, user.role.value)
    minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
    try:
        checkout = await stripe.checkout.Session.create_async(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
            success_url=f"{base}?checkout=success",
            cancel_url=f"{base}?checkout=cancelled",
            metadata={"user_id": str(user.id), "plan_id": str(plan.id)},
            subscription_data={"metadata": {"user_id": str(user.id), "plan_id": str(plan.id)}},
            idempotency_key=f"subcheckout_{user.id}_{plan.id}_{minute}",
        )
    except stripe.StripeError as exc:
        # Most often the plan's price belongs to a different Stripe account than the
        # key this process loaded — the plan needs re-syncing, not the user's attention.
        raise stripe_config_service.checkout_failed(
            exc, user_id=str(user.id), plan_id=str(plan.id)
        ) from exc
    log.info("subscription_checkout_created", user_id=str(user.id), plan_id=str(plan.id))
    return CheckoutResponse(checkout_url=str(checkout.url), session_id=str(checkout.id))


# ── webhook side ─────────────────────────────────────────────────────────────


def _apply_stripe_subscription(sub: Subscription, stripe_sub: Any) -> None:
    sub.stripe_subscription_id = str(_get(stripe_sub, "id") or sub.stripe_subscription_id)
    sub.status = _map_status(_get(stripe_sub, "status"))
    items = _get(_get(stripe_sub, "items"), "data") or []
    item = items[0] if items else None
    # Stripe moved the billing period from the subscription onto the subscription
    # item; older API versions still put it at the top level, so prefer that and
    # fall back to the item. Reading only the top level leaves both NULL, which
    # blanks the renewal dates and breaks the grace-period arithmetic.
    sub.current_period_start = _ts(
        _get(stripe_sub, "current_period_start") or _get(item, "current_period_start")
    )
    sub.current_period_end = _ts(
        _get(stripe_sub, "current_period_end") or _get(item, "current_period_end")
    )
    sub.cancel_at_period_end = bool(_get(stripe_sub, "cancel_at_period_end", False))
    sub.canceled_at = _ts(_get(stripe_sub, "canceled_at"))
    if item is not None:
        price = _get(item, "price")
        sub.stripe_price_id = str(_get(price, "id") or sub.stripe_price_id or "")
    pm = _get(stripe_sub, "default_payment_method")
    if pm is not None and not isinstance(pm, str):
        _apply_card(sub, pm)


def _apply_card(sub: Subscription, pm: Any) -> None:
    """Cache the card summary from an (expanded) PaymentMethod object."""
    card = _get(pm, "card")
    if card is None:
        return
    sub.payment_method_brand = str(_get(card, "brand") or "") or None
    sub.payment_method_last4 = str(_get(card, "last4") or "") or None
    sub.payment_method_exp_month = _get(card, "exp_month")
    sub.payment_method_exp_year = _get(card, "exp_year")


async def _refresh_card(
    session: AsyncSession, sub: Subscription, pm: Any, settings: Settings
) -> None:
    """``pm`` may be a PaymentMethod id (webhook payloads are not expanded) or an object."""
    if not pm:
        return
    if isinstance(pm, str):
        await _init_stripe(session, settings)
        pm = await stripe.PaymentMethod.retrieve_async(pm)
    _apply_card(sub, pm)


async def _sync_advisor_flag(session: AsyncSession, sub: Subscription) -> None:
    user = await session.get(User, sub.user_id)
    if user is None or user.role != UserRole.advisor:
        return
    profile = (
        await session.execute(select(AdvisorProfile).where(AdvisorProfile.user_id == user.id))
    ).scalar_one_or_none()
    if profile is None:
        return
    profile.subscription_status = sub.status.value
    if (
        profile.is_featured
        and sub.status in _ENDED
        and not await _free_plan_grants(session, "advisor", "featured_listing")
    ):
        # EPIC 04: ``featured_listing`` lapses with the subscription that granted it
        # (an ended subscription, not a past-due one still inside its grace days).
        profile.is_featured = False
    session.add(profile)


async def _find_by_stripe_id(session: AsyncSession, stripe_sub_id: str) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
        )
    ).scalar_one_or_none()


async def on_checkout_completed(session: AsyncSession, cs: Any, settings: Settings) -> None:
    """checkout.session.completed with mode=subscription: create the local row."""
    metadata = _get(cs, "metadata") or {}
    user_id = _get(metadata, "user_id")
    plan_id = _get(metadata, "plan_id")
    stripe_sub_id = _get(cs, "subscription")
    if not (user_id and plan_id and stripe_sub_id):
        log.warning("subscription_checkout_missing_metadata", session_id=str(_get(cs, "id")))
        return
    await _init_stripe(session, settings)
    stripe_sub = await stripe.Subscription.retrieve_async(
        str(stripe_sub_id), expand=["default_payment_method"]
    )
    sub = await _find_by_stripe_id(session, str(stripe_sub_id))
    if sub is None:
        sub = Subscription(
            user_id=uuid.UUID(str(user_id)),
            plan_id=uuid.UUID(str(plan_id)),
            stripe_subscription_id=str(stripe_sub_id),
            status=SubscriptionStatus.incomplete,
        )
        session.add(sub)
    _apply_stripe_subscription(sub, stripe_sub)
    customer = _get(cs, "customer")
    if customer:
        user = await session.get(User, sub.user_id)
        if user is not None and not user.stripe_customer_id:
            user.stripe_customer_id = str(customer)
            session.add(user)
    await _sync_advisor_flag(session, sub)
    await session.flush()


async def on_subscription_event(session: AsyncSession, stripe_sub: Any, settings: Settings) -> None:
    """customer.subscription.created / updated / deleted."""
    stripe_sub_id = str(_get(stripe_sub, "id") or "")
    sub = await _find_by_stripe_id(session, stripe_sub_id)
    if sub is None:
        metadata = _get(stripe_sub, "metadata") or {}
        user_id, plan_id = _get(metadata, "user_id"), _get(metadata, "plan_id")
        if not (user_id and plan_id):
            log.warning("subscription_event_unknown", stripe_subscription_id=stripe_sub_id)
            return
        sub = Subscription(
            user_id=uuid.UUID(str(user_id)),
            plan_id=uuid.UUID(str(plan_id)),
            stripe_subscription_id=stripe_sub_id,
        )
        session.add(sub)
    was = sub.status
    _apply_stripe_subscription(sub, stripe_sub)
    pm = _get(stripe_sub, "default_payment_method")
    if isinstance(pm, str):
        await _refresh_card(session, sub, pm, settings)
    if sub.status == SubscriptionStatus.canceled and was != SubscriptionStatus.canceled:
        if sub.access_until is None and sub.current_period_end is not None:
            sub.access_until = sub.current_period_end
        await notification_service.notify(
            session,
            user_id=sub.user_id,
            type=NotificationType.subscription_canceled,
            title="Subscription ended",
            body="Your subscription has ended. You can subscribe again at any time.",
            entity_type=NotificationEntityType.subscription,
            entity_id=sub.id,
        )
        user, plan = await _user_and_plan(session, sub)
        if user is not None:
            email_service.schedule_email(
                email_service.send_subscription_canceled_email(
                    to=user.email,
                    full_name=user.full_name or user.email,
                    plan_name=plan.name if plan else "your plan",
                    access_until=_utc(sub.access_until),
                    settings=settings,
                )
            )
    await _sync_advisor_flag(session, sub)
    await session.flush()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _user_and_plan(
    session: AsyncSession, sub: Subscription
) -> tuple[User | None, PricingPlan | None]:
    return await session.get(User, sub.user_id), await session.get(PricingPlan, sub.plan_id)


async def _sub_for_customer(session: AsyncSession, customer_id: str) -> Subscription | None:
    user = (
        await session.execute(select(User).where(User.stripe_customer_id == customer_id))
    ).scalar_one_or_none()
    if user is None:
        return None
    return await get_current(session, user.id)


async def on_customer_updated(session: AsyncSession, customer: Any, settings: Settings) -> None:
    """customer.updated: Billing Portal "update payment method" changes the
    customer's invoice default, not the subscription — refresh the card from it."""
    customer_id = str(_get(customer, "id") or "")
    pm = _get(_get(customer, "invoice_settings"), "default_payment_method")
    if not (customer_id and pm):
        return
    sub = await _sub_for_customer(session, customer_id)
    if sub is None:
        return
    await _refresh_card(session, sub, pm, settings)
    session.add(sub)
    await session.flush()


async def on_payment_method_attached(session: AsyncSession, pm: Any, settings: Settings) -> None:
    """payment_method.attached: a new card saved through the Billing Portal."""
    customer_id = str(_get(pm, "customer") or "")
    if not customer_id or _get(pm, "card") is None:
        return
    sub = await _sub_for_customer(session, customer_id)
    if sub is None:
        return
    _apply_card(sub, pm)
    session.add(sub)
    await session.flush()


def _invoice_subscription_id(invoice: Any) -> Any:
    """The subscription an invoice belongs to, across Stripe API versions.

    Up to 2025 this was ``invoice.subscription``. From ``2026-08-26`` the flat field
    is gone and it hangs off ``parent.subscription_details.subscription`` — reading
    only the old place makes every invoice look unrelated to any subscription, so the
    handler returns early and Billing History stays empty while the webhook is
    recorded as processed.
    """
    flat = _get(invoice, "subscription")
    if flat:
        return flat
    details = _get(_get(invoice, "parent"), "subscription_details")
    return _get(details, "subscription")


def _invoice_description(lines: list[Any], amount: Decimal) -> str | None:
    """The line that describes what this invoice actually did.

    Stripe orders a plan change credit-first, so ``lines[0]`` on an upgrade reads
    "Unused time on <old plan>" — printed in Billing History beside the charge it is
    the opposite of. Take the first line whose sign agrees with the invoice's own
    total instead; a one-line invoice is unaffected, which is every renewal.
    """
    if not lines:
        return None
    if amount > 0:
        for line in lines:
            if Decimal(str(_get(line, "amount", 0) or 0)) > 0:
                return str(_get(line, "description") or "") or None
    return str(_get(lines[0], "description") or "") or None


async def _upsert_invoice_row(
    session: AsyncSession, sub: Subscription, invoice: Any
) -> Decimal:
    """Write (or refresh) one Billing History row. Returns the amount paid.

    Deliberately silent: the webhook handler adds the notification and email on top,
    while a backfill of invoices already charged must not announce itself.
    """
    stripe_invoice_id = str(_get(invoice, "id") or "")
    lines = _get(_get(invoice, "lines"), "data") or []
    period_start = _ts(_get(invoice, "period_start"))
    period_end = _ts(_get(invoice, "period_end"))
    if lines:
        lp = _get(lines[0], "period")
        period_start = _ts(_get(lp, "start")) or period_start
        period_end = _ts(_get(lp, "end")) or period_end
    amount = Decimal(str(_get(invoice, "amount_paid", 0) or 0)) / Decimal(100)
    description = _invoice_description(lines, amount)
    existing = (
        await session.execute(
            select(SubscriptionInvoice).where(
                SubscriptionInvoice.stripe_invoice_id == stripe_invoice_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            SubscriptionInvoice(
                subscription_id=sub.id,
                # Snapshot, not a reference: the subscriber's plan at the moment of
                # the charge, so an upgrade never re-attributes what they paid before.
                plan_id=sub.plan_id,
                stripe_invoice_id=stripe_invoice_id,
                stripe_invoice_number=_get(invoice, "number"),
                description=(description or None),
                amount_usd=amount,
                status=str(_get(invoice, "status") or "paid"),
                period_start=period_start,
                period_end=period_end,
                hosted_invoice_url=_get(invoice, "hosted_invoice_url"),
                invoice_pdf_url=_get(invoice, "invoice_pdf"),
            )
        )
    else:
        existing.status = str(_get(invoice, "status") or "paid")
        existing.amount_usd = amount
        session.add(existing)
    return amount


async def reconcile_invoices(
    session: AsyncSession, sub: Subscription, settings: Settings
) -> None:
    """Rebuild Billing History from Stripe while we hold nothing for this subscription.

    A webhook that was missed, deduped or mishandled leaves the subscriber looking at
    an empty Billing History with no way to populate it. Reading the page is the
    natural moment to repair that, so nobody has to ask for a backfill. Once any row
    exists the webhook is keeping up and this stops running.
    """
    if not sub.stripe_subscription_id:
        return
    already = (
        await session.execute(
            select(SubscriptionInvoice.id)
            .where(SubscriptionInvoice.subscription_id == sub.id)
            .limit(1)
        )
    ).first()
    if already is not None:
        return
    await _init_stripe(session, settings)
    try:
        listed = await stripe.Invoice.list_async(
            subscription=str(sub.stripe_subscription_id), limit=100
        )
    except stripe.StripeError as exc:
        # Billing History degrades to empty; it must never break the page.
        log.warning(
            "invoice_backfill_failed",
            subscription_id=str(sub.id),
            error=str(exc)[:200],
        )
        return
    count = 0
    for invoice in _get(listed, "data") or []:
        await _upsert_invoice_row(session, sub, invoice)
        count += 1
    await session.flush()
    log.info("invoice_backfill", subscription_id=str(sub.id), imported=count)


async def on_invoice_paid(session: AsyncSession, invoice: Any, settings: Settings) -> None:
    stripe_sub_id = _invoice_subscription_id(invoice)
    if not stripe_sub_id:
        return
    sub = await _find_by_stripe_id(session, str(stripe_sub_id))
    if sub is None:
        log.warning("invoice_paid_unknown_subscription", stripe_subscription_id=str(stripe_sub_id))
        return
    period_start = _ts(_get(invoice, "period_start"))
    period_end = _ts(_get(invoice, "period_end"))
    lines = _get(_get(invoice, "lines"), "data") or []
    billing_line = next((line for line in lines if not _is_proration_line(line)), None)
    if billing_line is not None:
        lp = _get(billing_line, "period")
        period_start = _ts(_get(lp, "start")) or period_start
        period_end = _ts(_get(lp, "end")) or period_end
    elif lines:
        # Every line is a proration, so this invoice is a plan change *inside* a
        # period that has not moved. Its window runs from the moment of the change,
        # and writing that into the subscription both disagrees with Stripe and
        # restarts metering — ``_usage_row`` finds usage by ``period_start`` alone,
        # so the subscriber's counter would silently reset on every upgrade.
        # ``customer.subscription.updated`` owns these dates; leave them alone.
        period_start = period_end = None
    first_invoice = sub.status != SubscriptionStatus.active
    sub.status = SubscriptionStatus.active
    if period_start:
        sub.current_period_start = period_start
    if period_end:
        sub.current_period_end = period_end
    config = await payment_config_service.get_config(session)
    grace = timedelta(days=getattr(config, "subscription_grace_days", 3))
    if sub.current_period_end is not None:
        sub.access_until = sub.current_period_end + grace
    session.add(sub)

    amount = await _upsert_invoice_row(session, sub, invoice)
    await _sync_advisor_flag(session, sub)
    await session.flush()
    await notification_service.notify(
        session,
        user_id=sub.user_id,
        type=NotificationType.subscription_activated,
        title="Subscription active" if first_invoice else "Subscription renewed",
        body=f"Your payment of ${amount:.2f} was received.",
        entity_type=NotificationEntityType.subscription,
        entity_id=sub.id,
    )
    user, plan = await _user_and_plan(session, sub)
    if user is not None:
        email_service.schedule_email(
            email_service.send_subscription_activated_email(
                to=user.email,
                full_name=user.full_name or user.email,
                plan_name=plan.name if plan else "your plan",
                amount_usd=float(amount),
                period_end=_utc(sub.current_period_end),
                renewed=not first_invoice,
                invoice_url=_get(invoice, "hosted_invoice_url"),
                settings=settings,
            )
        )


async def on_invoice_payment_failed(
    session: AsyncSession, invoice: Any, settings: Settings
) -> None:
    stripe_sub_id = _invoice_subscription_id(invoice)
    if not stripe_sub_id:
        return
    sub = await _find_by_stripe_id(session, str(stripe_sub_id))
    if sub is None:
        return
    sub.status = SubscriptionStatus.past_due
    session.add(sub)
    await _sync_advisor_flag(session, sub)
    await session.flush()
    await notification_service.notify(
        session,
        user_id=sub.user_id,
        type=NotificationType.subscription_payment_failed,
        title="Subscription payment failed",
        body="We could not charge your card. Update your payment method to keep your plan.",
        entity_type=NotificationEntityType.subscription,
        entity_id=sub.id,
    )
    user, plan = await _user_and_plan(session, sub)
    if user is not None:
        email_service.schedule_email(
            email_service.send_subscription_payment_failed_email(
                to=user.email,
                full_name=user.full_name or user.email,
                plan_name=plan.name if plan else "your plan",
                manage_url=f"{settings.FRONTEND_URL.rstrip('/')}/{user.role.value}/subscription",
                settings=settings,
            )
        )


# ── self-service ─────────────────────────────────────────────────────────────


async def get_current(session: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    from app.services.entitlement_service import current_subscription

    sub = await current_subscription(session, user_id)
    if sub is not None:
        return sub
    return await _active_subscription(session, user_id)


async def get_current_for(
    session: AsyncSession, user: User, settings: Settings
) -> Subscription | None:
    """The user's subscription, reconciled from Stripe when we hold no row.

    Webhook delivery is best-effort: a wrong signing secret, an unreachable host, a
    deploy or a Stripe incident all end with the customer billed and our table still
    saying "free plan" — permanently, because nothing else ever asks. Reading is the
    natural place to repair that, so a missed event costs a page load instead of
    manual intervention. Stripe is only consulted when we have no row *and* the user
    already has a customer, so the common path stays a single query.
    """
    sub = await get_current(session, user.id)
    if sub is not None:
        return sub
    if not user.stripe_customer_id:
        return None
    reachable, found, imported = await _live_stripe_subscription(
        session, user, user.stripe_customer_id, settings
    )
    if imported is not None:
        return imported
    if reachable and not found:
        # Stripe says this customer pays for nothing, and we hold a row that still
        # claims otherwise. Only now is it safe to write that down.
        await _expire_lapsed_rows(session, user.id)
    return None


async def _expire_lapsed_rows(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Mark rows whose paid period is over as ``expired`` and let the flags follow.

    Reading is the only moment this becomes knowable: the sweep that would otherwise
    notice is not running, and Stripe's ending event is precisely the thing that
    failed to arrive. Writing the truth into the row rather than hiding it behind a
    read filter means every other reader — the admin list, the advisor's featured
    flag, the checkout guard — gets it without having to ask again.

    Only ever called once Stripe has confirmed the customer pays for nothing; a
    subscription must never be ended because Stripe was unreachable.
    """
    from app.services import entitlement_service

    days = await entitlement_service.grace_days(session)
    now = datetime.now(UTC)
    rows = list(
        (
            await session.execute(
                select(Subscription).where(
                    Subscription.user_id == user_id,
                    Subscription.status.in_(entitlement_service.LIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for sub in rows:
        if not entitlement_service.has_lapsed(sub, days, now):
            continue
        sub.status = SubscriptionStatus.expired
        if sub.access_until is None and sub.current_period_end is not None:
            sub.access_until = sub.current_period_end
        session.add(sub)
        await _sync_advisor_flag(session, sub)
        changed += 1
        log.info(
            "subscription_expired_on_read",
            subscription_id=str(sub.id),
            user_id=str(user_id),
            period_end=str(sub.current_period_end),
        )
    if changed:
        await session.flush()
    return changed


async def _require_stripe_sub(session: AsyncSession, user: User) -> Subscription:
    sub = await get_current(session, user.id)
    if sub is None or not sub.stripe_subscription_id:
        raise NotFoundError("No active subscription")
    return sub


async def cancel(
    session: AsyncSession, user: User, settings: Settings, *, at_period_end: bool = True
) -> Subscription:
    sub = await _require_stripe_sub(session, user)
    await _init_stripe(session, settings)
    if at_period_end:
        result = await stripe.Subscription.modify_async(
            str(sub.stripe_subscription_id),
            cancel_at_period_end=True,
            idempotency_key=f"subcancel_{sub.id}_{sub.current_period_end}",
        )
        sub.cancel_at_period_end = bool(_get(result, "cancel_at_period_end", True))
    else:
        result = await stripe.Subscription.cancel_async(str(sub.stripe_subscription_id))
        _apply_stripe_subscription(sub, result)
        sub.status = SubscriptionStatus.canceled
    session.add(sub)
    await session.flush()
    return sub


async def portal_url(session: AsyncSession, user: User, settings: Settings) -> str:
    customer_id = await ensure_customer(session, user, settings)
    await _init_stripe(session, settings)
    portal = await stripe.billing_portal.Session.create_async(
        customer=customer_id,
        return_url=f"{settings.FRONTEND_URL}/{user.role.value}/subscription/manage",
    )
    return str(portal.url)


async def _is_downgrade(session: AsyncSession, sub: Subscription, plan: PricingPlan) -> bool:
    """Cheaper than what they are on now. Both prices are ours, so no Stripe call."""
    current = await session.get(PricingPlan, sub.plan_id)
    if current is None:
        return False
    return Decimal(str(plan.price_usd)) < Decimal(str(current.price_usd))


async def change_plan(
    session: AsyncSession, user: User, plan_id: uuid.UUID, settings: Settings
) -> Subscription:
    sub = await _require_stripe_sub(session, user)
    plan = await _plan_for_purchase(session, user, plan_id)
    if plan.id == sub.plan_id:
        raise AppError("You are already on this plan", code="already_subscribed")
    await _init_stripe(session, settings)
    if Decimal(str(plan.price_usd)) <= 0 or not plan.stripe_price_id:
        # Downgrade to free: the paid subscription ends at period end.
        return await cancel(session, user, settings, at_period_end=True)
    if await _is_downgrade(session, sub, plan):
        # Client rule (2026-09-28): no mid-period downgrades. The subscriber keeps
        # what they paid for until the period ends, then chooses a plan afresh —
        # which also means entitlements never drop underneath someone's usage.
        raise AppError(
            "You can move to a lower plan once your current subscription ends",
            code="downgrade_not_allowed",
        )
    current = await stripe.Subscription.retrieve_async(str(sub.stripe_subscription_id))
    items = _get(_get(current, "items"), "data") or []
    if not items:
        raise AppError("Subscription has no billable item", code="invalid_state")
    try:
        result = await stripe.Subscription.modify_async(
            str(sub.stripe_subscription_id),
            items=[{"id": str(_get(items[0], "id")), "price": plan.stripe_price_id}],
            # Bill the prorated difference now: the upgrade is live immediately, so the
            # money moves immediately too. ``create_prorations`` would defer it to the
            # next invoice and make "Total due today" a lie.
            proration_behavior="always_invoice",
            metadata={"user_id": str(user.id), "plan_id": str(plan.id)},
            idempotency_key=f"subchange_{sub.id}_{plan.id}_{plan.price_version}",
        )
    except stripe.StripeError as exc:
        # ``always_invoice`` bills during this call, so a declined card surfaces here
        # rather than on a later invoice. Left uncaught it would be a 500.
        log.warning(
            "plan_change_failed",
            user_id=str(user.id),
            plan_id=str(plan.id),
            error=str(exc)[:300],
        )
        raise AppError(
            "Your plan could not be changed. Check your payment method and try again.",
            code="plan_change_failed",
        ) from exc
    sub.plan_id = plan.id
    _apply_stripe_subscription(sub, result)
    session.add(sub)
    await session.flush()
    return sub


def _is_proration_line(line: Any) -> bool:
    """Whether an invoice line is a proration, across Stripe API versions.

    Up to 2025 the flag sat on the line itself. From ``2026-08-26`` it moved to
    ``parent.subscription_item_details.proration`` and the old field disappeared —
    reading only the old one silently scores every line as non-proration and makes
    the change look free.
    """
    if _get(line, "proration", False):
        return True
    details = _get(_get(line, "parent"), "subscription_item_details")
    return bool(_get(details, "proration", False))


async def preview_change(
    session: AsyncSession, user: User, plan_id: uuid.UUID, settings: Settings
) -> ChangePlanPreviewRead:
    sub = await _require_stripe_sub(session, user)
    plan = await _plan_for_purchase(session, user, plan_id)
    brief = plan_brief(plan)
    if Decimal(str(plan.price_usd)) <= 0 or not plan.stripe_price_id:
        return ChangePlanPreviewRead(
            plan=brief,
            amount_due_today_usd=Decimal("0"),
            note="Your paid plan ends at the period end.",
            direction="cancel",
        )
    if await _is_downgrade(session, sub, plan):
        return ChangePlanPreviewRead(
            plan=brief,
            amount_due_today_usd=Decimal("0"),
            note=(
                "You keep your current plan until it ends, then you can choose this one. "
                "Cancel your subscription to stop it renewing."
            ),
            direction="downgrade",
        )
    await _init_stripe(session, settings)
    try:
        current = await stripe.Subscription.retrieve_async(str(sub.stripe_subscription_id))
        items = _get(_get(current, "items"), "data") or []
        preview = await stripe.Invoice.create_preview_async(
            customer=str(_get(current, "customer")),
            subscription=str(sub.stripe_subscription_id),
            subscription_details={
                "items": [{"id": str(_get(items[0], "id")), "price": plan.stripe_price_id}],
                "proration_behavior": "always_invoice",
            },
        )
        # Only the proration lines are what this change costs: under some
        # proration behaviours ``amount_due`` also carries the next period's charge.
        lines = _get(_get(preview, "lines"), "data") or []
        prorations = [line for line in lines if _is_proration_line(line)]
        cents = (
            sum(int(_get(line, "amount", 0) or 0) for line in prorations)
            if prorations
            # Nothing recognisable: trust Stripe's total rather than show 0.00.
            else int(_get(preview, "amount_due", 0) or 0)
        )
        return ChangePlanPreviewRead(
            plan=brief,
            amount_due_today_usd=Decimal(cents) / Decimal(100),
            note="Prorated for the rest of the current period.",
            direction="upgrade",
        )
    except stripe.StripeError as exc:
        log.warning("change_plan_preview_failed", error=str(exc)[:200])
        return ChangePlanPreviewRead(
            plan=brief,
            amount_due_today_usd=None,
            note="Stripe will prorate the change.",
            direction="upgrade",
        )


# ── reads ────────────────────────────────────────────────────────────────────


def plan_brief(plan: PricingPlan) -> SubscriptionPlanBrief:
    return SubscriptionPlanBrief(
        id=plan.id,
        name=plan.name,
        audience=plan.audience.value,
        price_usd=Decimal(str(plan.price_usd)),
        billing_interval=plan.billing_interval.value,
    )


def invoice_read(inv: SubscriptionInvoice) -> SubscriptionInvoiceRead:
    return SubscriptionInvoiceRead(
        id=inv.id,
        stripe_invoice_id=inv.stripe_invoice_id,
        invoice_number=inv.stripe_invoice_number,
        description=inv.description,
        amount_usd=Decimal(str(inv.amount_usd)),
        status=inv.status,
        period_start=inv.period_start,
        period_end=inv.period_end,
        hosted_invoice_url=inv.hosted_invoice_url,
        invoice_pdf_url=inv.invoice_pdf_url,
        created_at=inv.created_at,
    )


def invoices_stmt(subscription_id: uuid.UUID) -> Select[tuple[SubscriptionInvoice]]:
    return (
        select(SubscriptionInvoice)
        .where(SubscriptionInvoice.subscription_id == subscription_id)
        .order_by(SubscriptionInvoice.created_at.desc())
    )


async def read(session: AsyncSession, sub: Subscription) -> SubscriptionRead:
    plan = await session.get(PricingPlan, sub.plan_id)
    if plan is None:
        raise NotFoundError("Plan not found")
    latest = (await session.execute(invoices_stmt(sub.id).limit(1))).scalar_one_or_none()
    pm = (
        PaymentMethodSummary(
            brand=sub.payment_method_brand,
            last4=sub.payment_method_last4,
            exp_month=sub.payment_method_exp_month,
            exp_year=sub.payment_method_exp_year,
        )
        if sub.payment_method_last4
        else None
    )
    return SubscriptionRead(
        id=sub.id,
        plan=plan_brief(plan),
        status=sub.status.value,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        canceled_at=sub.canceled_at,
        access_until=sub.access_until,
        payment_method=pm,
        latest_invoice=invoice_read(latest) if latest else None,
    )


def _charged_total_subquery() -> Any:
    """Sum of settled invoices per subscription, as a correlated scalar.

    A subquery rather than a join so the list keeps one row per subscription, and
    ``coalesce`` so a subscription with no invoices reads 0 instead of NULL.
    """
    return (
        select(func.coalesce(func.sum(SubscriptionInvoice.amount_usd), 0))
        .where(
            SubscriptionInvoice.subscription_id == Subscription.id,
            SubscriptionInvoice.status == "paid",
        )
        .correlate(Subscription)
        .scalar_subquery()
    )


def admin_list_stmt(
    audience: str | None = None,
    status: str | None = None,
    q: str | None = None,
    grace_days: int = 3,
) -> Select[tuple[Subscription, User, PricingPlan, Decimal]]:
    """The admin list, filtered the way it is displayed.

    ``admin_read`` shows a lapsed row as Expired, so the filter has to agree: matching
    the stored column alone would list rows under Active that the same screen labels
    Expired, and hide them from Expired. The lapse is expressed as a date comparison
    rather than an interval so it means the same thing on Postgres and SQLite.
    """
    from app.services import entitlement_service

    stmt = (
        select(Subscription, User, PricingPlan, _charged_total_subquery().label("total_charged"))
        .join(User, User.id == Subscription.user_id)
        .join(PricingPlan, PricingPlan.id == Subscription.plan_id)
        .order_by(Subscription.created_at.desc())
    )
    if audience:
        stmt = stmt.where(PricingPlan.audience == audience)
    if status:
        wanted = SubscriptionStatus(status)
        lapsed = and_(
            Subscription.status.in_(entitlement_service.LIVE_STATUSES),
            Subscription.current_period_end.is_not(None),
            Subscription.current_period_end <= datetime.now(UTC) - timedelta(days=grace_days),
        )
        if wanted == SubscriptionStatus.expired:
            stmt = stmt.where(or_(Subscription.status == wanted, lapsed))
        elif wanted in entitlement_service.LIVE_STATUSES:
            stmt = stmt.where(Subscription.status == wanted, not_(lapsed))
        else:
            stmt = stmt.where(Subscription.status == wanted)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where((User.email.ilike(like)) | (User.full_name.ilike(like)))
    return stmt


def admin_invoices_stmt(subscription_id: uuid.UUID) -> Select[tuple[SubscriptionInvoice]]:
    """Every invoice behind the charged total, newest first."""
    return (
        select(SubscriptionInvoice)
        .where(SubscriptionInvoice.subscription_id == subscription_id)
        .order_by(SubscriptionInvoice.created_at.desc())
    )


async def admin_invoice_read(
    session: AsyncSession, row: SubscriptionInvoice, settings: Settings
) -> AdminInvoiceRead:
    """One invoice with the lines behind its total, fetched on demand.

    We store a single description per invoice, and on a proration invoice that is the
    credit line — so the stored text actively misleads next to the net amount. The
    lines are read from Stripe when an admin opens the sheet rather than duplicated
    into a child table. They are an explanation, never a dependency: if Stripe is
    unreachable the invoice still lists, just without its breakdown.
    """
    base = invoice_read(row).model_dump()
    lines: list[InvoiceLineRead] = []
    keys = await stripe_config_service.effective_keys(session, settings)
    if row.stripe_invoice_id and keys.secret_key:
        stripe.api_key = keys.secret_key
        try:
            listed = await stripe.Invoice.list_lines_async(row.stripe_invoice_id, limit=20)
            for line in _get(listed, "data") or []:
                lines.append(
                    InvoiceLineRead(
                        description=_get(line, "description"),
                        amount_usd=Decimal(str(_get(line, "amount", 0) or 0)) / Decimal(100),
                    )
                )
        except stripe.StripeError as exc:
            log.warning(
                "invoice_lines_unavailable",
                stripe_invoice_id=row.stripe_invoice_id,
                error=str(exc)[:200],
            )
    return AdminInvoiceRead(**base, lines=lines)


def admin_read(
    sub: Subscription,
    user: User,
    plan: PricingPlan,
    total_charged: Decimal | float | int | None = None,
    grace_days: int = 3,
) -> AdminSubscriptionRead:
    """One row for the admin list.

    ``status`` is derived rather than copied: a row still saying ``active`` beside a
    renewal date in the past was the screen contradicting itself, and the admin has
    no way to tell which half is true. ``_expire_lapsed_rows`` corrects the stored
    status when the subscriber's own page is read, but the admin list must not wait
    for that to happen.
    """
    from app.services import entitlement_service

    status = sub.status.value
    if sub.status in entitlement_service.LIVE_STATUSES and entitlement_service.has_lapsed(
        sub, grace_days, datetime.now(UTC)
    ):
        status = SubscriptionStatus.expired.value
    return AdminSubscriptionRead(
        id=sub.id,
        user_id=user.id,
        user_email=user.email,
        user_name=user.full_name,
        audience=plan.audience.value,
        plan_id=plan.id,
        plan_name=plan.name,
        price_usd=Decimal(str(plan.price_usd)),
        total_charged_usd=Decimal(str(total_charged or 0)),
        status=status,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        stripe_subscription_id=sub.stripe_subscription_id,
        created_at=sub.created_at,
    )


async def admin_cancel(
    session: AsyncSession, subscription_id: uuid.UUID, settings: Settings
) -> Subscription:
    sub = await session.get(Subscription, subscription_id)
    if sub is None:
        raise NotFoundError("Subscription not found")
    if sub.stripe_subscription_id and sub.status in _ACTIVE_LIKE:
        await _init_stripe(session, settings)
        try:
            result = await stripe.Subscription.cancel_async(str(sub.stripe_subscription_id))
        except stripe.InvalidRequestError as exc:
            # Stripe has already ended it, or never had it. The admin asked for it to
            # be over and it is over, so finish locally rather than refusing — this is
            # exactly the lapsed row an admin is most likely to be cleaning up.
            log.info(
                "admin_cancel_already_ended",
                subscription_id=str(sub.id),
                stripe_subscription_id=str(sub.stripe_subscription_id),
                error=str(exc)[:200],
            )
        except stripe.StripeError as exc:
            # Anything else — auth, network, rate limit — left the remote side in an
            # unknown state, so do not record a cancellation that may not have happened.
            log.warning(
                "admin_cancel_stripe_error",
                subscription_id=str(sub.id),
                error=str(exc)[:300],
            )
            raise AppError(
                "Stripe could not cancel this subscription; try again",
                code="subscription_cancel_failed",
            ) from exc
        else:
            _apply_stripe_subscription(sub, result)
    sub.status = SubscriptionStatus.canceled
    sub.canceled_at = sub.canceled_at or datetime.now(UTC)
    sub.access_until = datetime.now(UTC)
    session.add(sub)
    await _sync_advisor_flag(session, sub)
    await session.flush()
    return sub


# ── Subscriptions Finance summary (admin cards) ──────────────────────────────

_PAID_INVOICE_STATUS = "paid"


def _period_bounds(period: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """[start, end) for the selected period. ``overall`` is unbounded on both ends."""
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return start, end
    if period == "yearly":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, start.replace(year=start.year + 1)
    return None, None


def _previous_bounds(period: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """The window the change percentage compares against. ``overall`` has none."""
    if period == "daily":
        start, _ = _period_bounds(period, now)
        if start is None:
            return None, None
        return start - timedelta(days=1), start
    if period == "monthly":
        start, _ = _period_bounds(period, now)
        if start is None:
            return None, None
        prev_start = (start - timedelta(days=1)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return prev_start, start
    if period == "yearly":
        start, _ = _period_bounds(period, now)
        if start is None:
            return None, None
        return start.replace(year=start.year - 1), start
    return None, None


def _change_pct(current: float, previous: float) -> float:
    """Percent change, with the zero-baseline convention Financial Analytics uses."""
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round(100.0 * (current - previous) / previous, 2)


def _invoice_window_clause(start: datetime | None, end: datetime | None) -> list[Any]:
    """Attribute an invoice by the billing period it covers.

    ``period_start`` is the month the money was for; ``created_at`` is when our row
    was written, which for a backfilled invoice is import time rather than payment
    time. Older rows can carry no period, so they fall back to ``created_at``.
    """
    if start is None or end is None:
        return []
    when = func.coalesce(SubscriptionInvoice.period_start, SubscriptionInvoice.created_at)
    return [when >= start, when < end]


def _revenue_stmt(start: datetime | None, end: datetime | None) -> Select[Any]:
    """Paid revenue in the window, grouped by the plan each invoice was charged for.

    The join is on ``SubscriptionInvoice.plan_id`` -- the plan snapshotted at the time
    of the charge -- never through the subscription, whose ``plan_id`` is wherever the
    subscriber has since moved. Outer-joined so an unattributed legacy row still
    reaches the totals instead of being silently dropped.

    Returned rather than executed so it can be compiled against the Postgres dialect
    in a test: the suite runs on SQLite, which cannot see a Postgres type error.
    """
    return (
        select(
            SubscriptionInvoice.plan_id,
            func.coalesce(PricingPlan.name, "Unattributed"),
            # Selected raw, never COALESCEd: `audience` is a Postgres enum and
            # `COALESCE(enum, '')` is a DatatypeMismatchError there, while SQLite --
            # which the tests run on -- treats it as text and passes. `_audience_value`
            # turns the NULL from the outer join into "" in Python instead.
            PricingPlan.audience,
            func.coalesce(func.sum(SubscriptionInvoice.amount_usd), 0.0),
        )
        .select_from(SubscriptionInvoice)
        .outerjoin(PricingPlan, PricingPlan.id == SubscriptionInvoice.plan_id)
        .where(SubscriptionInvoice.status == _PAID_INVOICE_STATUS)
        .where(*_invoice_window_clause(start, end))
        .group_by(SubscriptionInvoice.plan_id, PricingPlan.name, PricingPlan.audience)
    )


async def _revenue_rows(
    session: AsyncSession, start: datetime | None, end: datetime | None
) -> list[tuple[uuid.UUID | None, str, str, float]]:
    rows = (await session.execute(_revenue_stmt(start, end))).all()
    return [(r[0], str(r[1]), _audience_value(r[2]), round(float(r[3] or 0), 2)) for r in rows]


def _audience_value(audience: object) -> str:
    return str(getattr(audience, "value", audience) or "")


def _sum_revenue(
    rows: list[tuple[uuid.UUID | None, str, str, float]], audience: str | None
) -> float:
    return round(
        sum(amount for _id, _name, aud, amount in rows if audience is None or aud == audience),
        2,
    )


# How many gaps one page load will repair. A missed webhook is rare; a screen that
# fires an unbounded number of Stripe calls on every load is not acceptable, so the
# backlog is worked a page at a time and catches up over a few visits.
_MAX_REPAIRS_PER_LOAD = 25


async def _repair_missing_invoices(session: AsyncSession, settings: Settings) -> None:
    """Import invoices for subscriptions we hold none for.

    The seeker's own Billing History already does this (``reconcile_invoices``), so a
    missed ``invoice.paid`` costs that subscriber a page load. The admin had no such
    path: a seeker who had genuinely paid $100 showed as an Active subscriber beside
    $0 in every revenue card, with nothing on the screen to click.

    Deliberately best effort. ``reconcile_invoices`` already returns early when any row
    exists and swallows Stripe errors, and anything else -- Stripe unconfigured, an
    outage -- is caught here, because a repair is a bonus and never a dependency. The
    page renders either way.
    """
    missing = (
        (
            await session.execute(
                select(Subscription)
                .outerjoin(
                    SubscriptionInvoice,
                    SubscriptionInvoice.subscription_id == Subscription.id,
                )
                .where(
                    Subscription.stripe_subscription_id.is_not(None),
                    SubscriptionInvoice.id.is_(None),
                )
                .order_by(Subscription.created_at.desc())
                .limit(_MAX_REPAIRS_PER_LOAD)
            )
        )
        .scalars()
        .all()
    )
    for sub in missing:
        try:
            await reconcile_invoices(session, sub, settings)
        except Exception as exc:  # noqa: BLE001 - a repair must never break the screen
            log.warning(
                "admin_summary_repair_failed",
                subscription_id=str(sub.id),
                error=str(exc)[:200],
            )
            return


async def admin_finance_summary(
    session: AsyncSession, period: str = "monthly", settings: Settings | None = None
) -> AdminSubscriptionSummaryRead:
    """The cards above the Subscriptions Finance table.

    Counts are "right now"; revenue is money received inside ``period``. They are
    deliberately different questions, which is why only the revenue row carries a
    change figure.
    """
    # Imported in-function like every other use here: the two modules import each other.
    from app.services import entitlement_service

    if period not in ("daily", "monthly", "yearly", "overall"):
        raise AppError("Unknown period", code="invalid_period")

    # Repair before counting, so a webhook that never arrived does not read as $0.
    if settings is not None:
        await _repair_missing_invoices(session, settings)

    now = datetime.now(UTC)
    config = await payment_config_service.get_config(session)
    grace_days = int(config.subscription_grace_days)

    # Counts follow the same lapse rule as the table (`admin_list_stmt`): a row whose
    # paid period plus grace has passed reads Expired there, so it must not read
    # Active here or the card contradicts the rows underneath it.
    lapsed = and_(
        Subscription.current_period_end.is_not(None),
        Subscription.current_period_end <= now - timedelta(days=grace_days),
    )
    live = (Subscription.status.in_(entitlement_service.LIVE_STATUSES), not_(lapsed))

    count_rows = (
        await session.execute(
            select(PricingPlan.audience, func.count(Subscription.id))
            .join(PricingPlan, PricingPlan.id == Subscription.plan_id)
            .where(*live)
            .group_by(PricingPlan.audience)
        )
    ).all()
    counts = {_audience_value(a): int(n or 0) for a, n in count_rows}
    active_seekers = counts.get("seeker", 0)
    active_advisors = counts.get("advisor", 0)

    start, end = _period_bounds(period, now)
    current = await _revenue_rows(session, start, end)

    prev_start, prev_end = _previous_bounds(period, now)
    has_previous = prev_start is not None
    previous = await _revenue_rows(session, prev_start, prev_end) if has_previous else []
    prev_by_plan = {plan_id: amount for plan_id, _name, _aud, amount in previous}

    total = _sum_revenue(current, None)
    advisor = _sum_revenue(current, "advisor")
    seeker = _sum_revenue(current, "seeker")
    prev_total = _sum_revenue(previous, None)
    prev_advisor = _sum_revenue(previous, "advisor")
    prev_seeker = _sum_revenue(previous, "seeker")

    # Subscribers per plan, counted the same lapse-aware way as the headline.
    sub_rows = (
        await session.execute(
            select(Subscription.plan_id, func.count(Subscription.id))
            .where(*live)
            .group_by(Subscription.plan_id)
        )
    ).all()
    subs_by_plan = {pid: int(n or 0) for pid, n in sub_rows}

    plans = [
        AdminSubscriptionPlanRevenueRead(
            plan_id=plan_id,
            plan_name=name,
            audience="advisor" if aud == "advisor" else "seeker",
            revenue_usd=amount,
            change_pct=(
                _change_pct(amount, prev_by_plan.get(plan_id, 0.0)) if has_previous else None
            ),
            subscriber_count=subs_by_plan.get(plan_id, 0),
        )
        for plan_id, name, aud, amount in current
        if aud in ("advisor", "seeker")
    ]
    # Best first within each audience: the row exists to answer which plan earns most.
    plans.sort(key=lambda p: (p.audience, -p.revenue_usd, p.plan_name))

    return AdminSubscriptionSummaryRead(
        period=period,
        active_subscribers=active_seekers + active_advisors,
        active_seekers=active_seekers,
        active_advisors=active_advisors,
        total_revenue_usd=total,
        total_revenue_change_pct=_change_pct(total, prev_total) if has_previous else None,
        advisor_revenue_usd=advisor,
        advisor_revenue_change_pct=_change_pct(advisor, prev_advisor) if has_previous else None,
        seeker_revenue_usd=seeker,
        seeker_revenue_change_pct=_change_pct(seeker, prev_seeker) if has_previous else None,
        plans=plans,
    )
