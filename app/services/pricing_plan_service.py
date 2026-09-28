"""Pricing plans and their Stripe sync (EPIC 04, PAY-112 / PAY-113 / PAY-114).

Stripe Product / Price creation is idempotent per plan (``plan_product_{id}``,
``plan_price_{id}_v{n}``), so a retry after a failure never creates a duplicate.
Prices are immutable on Stripe: a price change creates a new Price and archives the
old one; existing subscribers keep their old price id until they change plan.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import stripe
import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, NotFoundError
from app.models.notification import NotificationType
from app.models.pricing_plan import (
    BillingInterval,
    FeatureKind,
    FeatureValueType,
    PlanAudience,
    PlanFeatureCatalog,
    PlanStatus,
    PricingPlan,
    PricingPlanFeature,
)
from app.models.subscription import Subscription, SubscriptionStatus
from app.schemas.pricing_plan import (
    FeatureCatalogRead,
    PlanFeatureInput,
    PlanFeatureRead,
    PricingPlanAdminRead,
    PricingPlanCreate,
    PricingPlanPublicRead,
    PricingPlanUpdate,
)
from app.services import notification_service

log = structlog.get_logger()

# One enforceable key per project module. The code owns this list and the gate for
# each key; the admin decides per plan whether a key is included and with what value.
CATALOG: tuple[dict[str, object], ...] = (
    {
        "key": "ai_assessments",
        "label": "AI assessments per month",
        "module": "ai_assessment",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 10,
    },
    {
        "key": "ai_insights",
        "label": "AI insights on results",
        "module": "ai_assessment",
        "audience": "seeker",
        "limit_type": "bool",
        "display_order": 20,
    },
    {
        "key": "assessment_history",
        "label": "Assessment history",
        "module": "ai_assessment",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 30,
    },
    {
        "key": "matched_advisors",
        "label": "Recommended advisors",
        "module": "matching",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 40,
    },
    {
        "key": "find_advisor",
        "label": "Find Advisor search",
        "module": "advisors",
        "audience": "seeker",
        "limit_type": "bool",
        "display_order": 50,
    },
    {
        "key": "bookmarks",
        "label": "Bookmarked advisors",
        "module": "bookmarks",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 60,
    },
    {
        "key": "consultations",
        "label": "Consultations per month",
        "module": "bookings",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 70,
    },
    {
        "key": "chats",
        "label": "Chat with advisors",
        "module": "conversations",
        "audience": "seeker",
        "limit_type": "bool",
        "display_order": 80,
    },
    {
        "key": "documents",
        "label": "Document uploads",
        "module": "documents",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 90,
    },
    {
        "key": "visa_journey",
        "label": "Visa Journey",
        "module": "visa_journey",
        "audience": "seeker",
        "limit_type": "bool",
        "display_order": 100,
    },
    {
        "key": "priority_support",
        "label": "Priority support",
        "module": "support",
        "audience": "both",
        "limit_type": "bool",
        "display_order": 110,
    },
    {
        "key": "leads",
        "label": "Leads per month",
        "module": "leads",
        "audience": "advisor",
        "limit_type": "number",
        "display_order": 120,
    },
    {
        "key": "featured_listing",
        "label": "Featured listing",
        "module": "advisors",
        "audience": "advisor",
        "limit_type": "bool",
        "display_order": 130,
    },
    {
        "key": "client_bookings",
        "label": "Client bookings per month",
        "module": "bookings",
        "audience": "advisor",
        "limit_type": "number",
        "display_order": 140,
    },
)

_CENT = Decimal("0.01")


def _val(value: object) -> str:
    """Enum member or raw string (before flush) -> its string value."""
    return str(getattr(value, "value", value))


# ── catalog ──────────────────────────────────────────────────────────────────


async def get_catalog(session: AsyncSession) -> list[PlanFeatureCatalog]:
    """The seeded catalog; inserts any key the migration seed is missing."""
    rows = list((await session.execute(select(PlanFeatureCatalog))).scalars().all())
    known = {r.key for r in rows}
    missing = [PlanFeatureCatalog(**entry) for entry in CATALOG if entry["key"] not in known]
    if missing:
        session.add_all(missing)
        await session.flush()
        rows.extend(missing)
    rows.sort(key=lambda r: r.display_order)
    return rows


def catalog_read(row: PlanFeatureCatalog) -> FeatureCatalogRead:
    return FeatureCatalogRead(
        key=row.key,
        label=row.label,
        module=row.module,
        audience=row.audience,
        limit_type=row.limit_type,
        display_order=row.display_order,
    )


async def _validate_features(
    session: AsyncSession, audience: PlanAudience, features: list[PlanFeatureInput]
) -> list[PricingPlanFeature]:
    catalog = {c.key: c for c in await get_catalog(session)}
    seen: set[str] = set()
    out: list[PricingPlanFeature] = []
    for f in features:
        if f.kind == "enforceable":
            entry = catalog.get(f.feature_key or "")
            if entry is None:
                raise AppError(f"Unknown feature key '{f.feature_key}'", code="unknown_feature_key")
            if entry.audience not in ("both", audience.value):
                raise AppError(
                    f"Feature '{f.feature_key}' is not available to {audience.value} plans",
                    code="feature_audience_mismatch",
                )
            if f.value_type != entry.limit_type:
                raise AppError(
                    f"Feature '{f.feature_key}' must be of type {entry.limit_type}",
                    code="feature_type_mismatch",
                )
            if f.feature_key in seen:
                raise AppError(f"Feature '{f.feature_key}' listed twice", code="duplicate_feature")
            seen.add(f.feature_key or "")
        out.append(
            PricingPlanFeature(
                kind=FeatureKind(f.kind),
                feature_key=f.feature_key,
                label=f.label,
                value_type=FeatureValueType(f.value_type),
                value=f.value,
                is_enabled=f.is_enabled,
                display_order=f.display_order,
            )
        )
    return out


# ── Stripe sync ──────────────────────────────────────────────────────────────


def _init_stripe() -> None:
    settings = get_settings()
    if not settings.STRIPE_SECRET_KEY:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    stripe.api_key = settings.STRIPE_SECRET_KEY


def _metadata(plan: PricingPlan) -> dict[str, str]:
    feats = ",".join(
        f"{f.feature_key}={f.value or 'on' if f.is_enabled else 'off'}"
        for f in plan.features
        if f.kind == FeatureKind.enforceable
    )
    return {
        "plan_id": str(plan.id),
        "audience": _val(plan.audience),
        "price_version": str(plan.price_version),
        "features": feats[:480],
    }


def _description_kwarg(plan: PricingPlan) -> dict[str, Any]:
    """Description is optional here but cannot be blank on Stripe.

    Stripe reads an empty string as "unset this field" and refuses it outright, so a
    plan saved without a description has to leave the key out of the call rather than
    send "". On an edit that clears the text the same rule applies: Stripe keeps
    whatever description the product already had, since it cannot be removed.
    """
    description = (plan.description or "").strip()
    return {"description": description} if description else {}


def _unit_amount(price: Decimal) -> int:
    return int((Decimal(str(price)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


async def _sync_to_stripe(session: AsyncSession, plan: PricingPlan) -> None:
    """Create whatever Stripe object is missing; never duplicates thanks to the keys."""
    if Decimal(str(plan.price_usd)) <= 0:
        plan.status = PlanStatus.active
        plan.sync_error = None
        plan.last_synced_at = datetime.now(UTC)
        return
    _init_stripe()
    try:
        if not plan.stripe_product_id:
            product = await stripe.Product.create_async(
                name=plan.name,
                metadata=_metadata(plan),
                idempotency_key=f"plan_product_{plan.id}",
                **_description_kwarg(plan),
            )
            plan.stripe_product_id = str(product.id)
            await session.flush()
        if not plan.stripe_price_id:
            version = plan.price_version or 1
            price = await stripe.Price.create_async(
                product=plan.stripe_product_id,
                unit_amount=_unit_amount(plan.price_usd),
                currency=plan.currency,
                recurring={"interval": _val(plan.billing_interval)},  # type: ignore[typeddict-item]
                metadata={"plan_id": str(plan.id), "version": str(version)},
                idempotency_key=f"plan_price_{plan.id}_v{version}",
            )
            plan.stripe_price_id = str(price.id)
            plan.price_version = version
        plan.status = PlanStatus.active
        plan.sync_error = None
        plan.last_synced_at = datetime.now(UTC)
    except stripe.StripeError as exc:
        plan.status = PlanStatus.draft
        plan.sync_error = str(exc)[:500]
        log.warning("plan_sync_failed", plan_id=str(plan.id), error=plan.sync_error)
        await notification_service.notify_admins(
            session,
            type=NotificationType.plan_sync_failed,
            title="Pricing plan sync failed",
            body=f"Plan '{plan.name}' could not be synced to Stripe: {plan.sync_error}",
        )


# ── CRUD ─────────────────────────────────────────────────────────────────────


async def get(session: AsyncSession, plan_id: uuid.UUID) -> PricingPlan:
    plan = await session.get(PricingPlan, plan_id)
    if plan is None:
        raise NotFoundError("Pricing plan not found")
    return plan


async def create(
    session: AsyncSession, data: PricingPlanCreate, admin_id: uuid.UUID
) -> PricingPlan:
    audience = PlanAudience(data.audience)
    features = await _validate_features(session, audience, data.features)
    plan = PricingPlan(
        audience=audience,
        name=data.name,
        description=data.description,
        tagline=data.tagline,
        price_usd=Decimal(str(data.price_usd)).quantize(_CENT),
        billing_interval=BillingInterval(data.billing_interval),
        is_highlighted=data.is_highlighted,
        status=PlanStatus.draft,
        price_version=1,
        created_by=admin_id,
        features=features,
    )
    session.add(plan)
    await session.flush()
    await _sync_to_stripe(session, plan)
    session.add(plan)
    await session.flush()
    await session.refresh(plan)
    return plan


async def retry_sync(session: AsyncSession, plan_id: uuid.UUID, admin_id: uuid.UUID) -> PricingPlan:
    plan = await get(session, plan_id)
    if plan.status == PlanStatus.inactive:
        raise AppError("Activate the plan before syncing it", code="plan_inactive")
    await _sync_to_stripe(session, plan)
    plan.updated_by = admin_id
    session.add(plan)
    await session.flush()
    await session.refresh(plan)
    return plan


async def update(
    session: AsyncSession, plan_id: uuid.UUID, data: PricingPlanUpdate, admin_id: uuid.UUID
) -> PricingPlan:
    plan = await get(session, plan_id)
    fields = data.model_dump(exclude_unset=True)
    price_changed = (
        "price_usd" in fields
        and fields["price_usd"] is not None
        and Decimal(str(fields["price_usd"])).quantize(_CENT) != Decimal(str(plan.price_usd))
    )
    text_changed = False
    for key in ("name", "description", "tagline", "is_highlighted"):
        if key in fields and getattr(plan, key) != fields[key]:
            setattr(plan, key, fields[key])
            text_changed = True
    if data.features is not None:
        plan.features = await _validate_features(session, plan.audience, data.features)
        text_changed = True
    plan.updated_by = admin_id
    await session.flush()

    if price_changed:
        old_price_id = plan.stripe_price_id
        plan.price_usd = Decimal(str(fields["price_usd"])).quantize(_CENT)
        plan.price_version = (plan.price_version or 0) + 1
        plan.stripe_price_id = None  # a new Price is created by the sync below
        await session.flush()
        if Decimal(str(plan.price_usd)) <= 0:
            plan.status = PlanStatus.active
        else:
            await _sync_to_stripe(session, plan)
        if old_price_id:
            await _archive_price(old_price_id)
    elif text_changed and plan.stripe_product_id:
        _init_stripe()
        try:
            await stripe.Product.modify_async(
                plan.stripe_product_id,
                name=plan.name,
                metadata=_metadata(plan),
                **_description_kwarg(plan),
            )
            plan.last_synced_at = datetime.now(UTC)
            plan.sync_error = None
        except stripe.StripeError as exc:
            plan.sync_error = str(exc)[:500]
            log.warning("plan_metadata_sync_failed", plan_id=str(plan.id), error=plan.sync_error)
    session.add(plan)
    await session.flush()
    await session.refresh(plan)
    return plan


async def _archive_price(price_id: str) -> None:
    _init_stripe()
    try:
        await stripe.Price.modify_async(price_id, active=False)
    except stripe.StripeError as exc:
        log.warning("price_archive_failed", price_id=price_id, error=str(exc)[:200])


async def set_active(
    session: AsyncSession, plan_id: uuid.UUID, active: bool, admin_id: uuid.UUID
) -> PricingPlan:
    """Deactivate archives the Stripe objects (never deletes); activate restores them."""
    plan = await get(session, plan_id)
    if plan.stripe_product_id or plan.stripe_price_id:
        _init_stripe()
        try:
            if plan.stripe_product_id:
                await stripe.Product.modify_async(plan.stripe_product_id, active=active)
            if plan.stripe_price_id:
                await stripe.Price.modify_async(plan.stripe_price_id, active=active)
        except stripe.StripeError as exc:
            raise AppError(
                f"Stripe refused the change: {str(exc)[:200]}", code="plan_sync_failed"
            ) from exc
    if active:
        plan.status = (
            PlanStatus.active
            if Decimal(str(plan.price_usd)) <= 0 or plan.stripe_price_id
            else PlanStatus.draft
        )
    else:
        plan.status = PlanStatus.inactive
    plan.updated_by = admin_id
    session.add(plan)
    await session.flush()
    await session.refresh(plan)
    return plan


# ── reads ────────────────────────────────────────────────────────────────────


def list_admin_stmt(
    audience: str | None = None, status: str | None = None
) -> Select[tuple[PricingPlan]]:
    stmt = select(PricingPlan).order_by(PricingPlan.audience, PricingPlan.price_usd)
    if audience:
        stmt = stmt.where(PricingPlan.audience == PlanAudience(audience))
    if status:
        stmt = stmt.where(PricingPlan.status == PlanStatus(status))
    return stmt


async def list_public(session: AsyncSession, audience: str) -> list[PricingPlan]:
    stmt = (
        select(PricingPlan)
        .where(
            PricingPlan.audience == PlanAudience(audience),
            PricingPlan.status == PlanStatus.active,
        )
        .order_by(PricingPlan.price_usd)
    )
    return list((await session.execute(stmt)).scalars().all())


async def free_plan_for(session: AsyncSession, audience: str) -> PricingPlan | None:
    """The active zero-price plan for an audience, if the admin configured one."""
    stmt = (
        select(PricingPlan)
        .where(
            PricingPlan.audience == PlanAudience(audience),
            PricingPlan.status == PlanStatus.active,
            PricingPlan.price_usd <= 0,
        )
        .order_by(PricingPlan.created_at)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def feature_read(f: PricingPlanFeature) -> PlanFeatureRead:
    return PlanFeatureRead(
        id=f.id,
        kind=_val(f.kind),
        feature_key=f.feature_key,
        label=f.label,
        value_type=_val(f.value_type),
        value=f.value,
        is_enabled=f.is_enabled,
        display_order=f.display_order,
    )


def public_read(plan: PricingPlan) -> PricingPlanPublicRead:
    return PricingPlanPublicRead(
        id=plan.id,
        audience=_val(plan.audience),
        name=plan.name,
        description=plan.description,
        tagline=plan.tagline,
        price_usd=Decimal(str(plan.price_usd)),
        currency=plan.currency,
        billing_interval=_val(plan.billing_interval),
        is_highlighted=plan.is_highlighted,
        is_free=Decimal(str(plan.price_usd)) <= 0,
        features=[feature_read(f) for f in plan.features],
    )


async def admin_read(session: AsyncSession, plan: PricingPlan) -> PricingPlanAdminRead:
    subscribers = (
        await session.scalar(
            select(func.count()).where(
                Subscription.plan_id == plan.id,
                Subscription.status.in_(
                    [
                        SubscriptionStatus.active,
                        SubscriptionStatus.trialing,
                        SubscriptionStatus.past_due,
                    ]
                ),
            )
        )
    ) or 0
    base = public_read(plan)
    return PricingPlanAdminRead(
        **base.model_dump(),
        status=_val(plan.status),
        stripe_product_id=plan.stripe_product_id,
        stripe_price_id=plan.stripe_price_id,
        price_version=plan.price_version,
        sync_error=plan.sync_error,
        last_synced_at=plan.last_synced_at,
        active_subscribers=int(subscribers),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )
