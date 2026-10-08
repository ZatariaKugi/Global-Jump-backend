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
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
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
        # Counted when an assessment is COMPLETED (results and insights shown), not
        # when it is started; the AI narrative is part of the assessment, not a perk.
        "key": "ai_assessments",
        "label": "AI assessments per month",
        "module": "ai_assessment",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 10,
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
        "key": "bookmarks",
        "label": "Bookmarked advisors",
        "module": "bookmarks",
        "audience": "seeker",
        "limit_type": "number",
        "display_order": 60,
    },
    {
        "key": "chats",
        "label": "Chat",
        "module": "conversations",
        "audience": "both",
        "limit_type": "bool",
        "display_order": 80,
    },
    {
        "key": "documents",
        "label": "Documents",
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
        "key": "support",
        "label": "Support",
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
        "key": "profile_edits",
        "label": "Profile edits",
        "module": "advisors",
        "audience": "advisor",
        "limit_type": "number",
        "display_order": 125,
    },
    {
        "key": "ai_recommended",
        "label": "Appears in AI recommendations",
        "module": "matching",
        "audience": "advisor",
        "limit_type": "bool",
        "display_order": 135,
    },
)
CATALOG_KEYS: frozenset[str] = frozenset(str(entry["key"]) for entry in CATALOG)

# Keys retired on 2026-10-08. Plans written before then may still carry the old
# spelling; ``canonical_key`` maps it forward and ``get_catalog`` rewrites the rows.
RENAMED_KEYS: dict[str, str] = {"priority_support": "support"}

# What the admin can never put behind a plan: the marketplace transaction itself
# (discussion sections 33-36). Shown read-only in the plan editor.
CORE_FEATURES: tuple[tuple[str, str], ...] = (
    ("consultations", "Consultation"),
    ("bookings", "Booking"),
    ("appointments", "Appointments"),
    ("availability", "Availability"),
    ("booking_management", "Booking management"),
)


def canonical_key(key: str | None) -> str | None:
    """A stored feature key under its current name."""
    if key is None:
        return None
    return RENAMED_KEYS.get(key, key)


_CENT = Decimal("0.01")


def _val(value: object) -> str:
    """Enum member or raw string (before flush) -> its string value."""
    return str(getattr(value, "value", value))


# ── catalog ──────────────────────────────────────────────────────────────────


async def get_catalog(session: AsyncSession) -> list[PlanFeatureCatalog]:
    """The seeded catalog, kept equal to ``CATALOG``.

    Inserts any key the migration seed is missing, applies the renames to stored
    rows, and drops retired keys so an environment migrated from an older catalog
    never keeps selling a perk the code no longer gates.
    """
    rows = list((await session.execute(select(PlanFeatureCatalog))).scalars().all())
    changed = False
    for old, new in RENAMED_KEYS.items():
        await session.execute(
            sa_update(PricingPlanFeature)
            .where(PricingPlanFeature.feature_key == old)
            .values(feature_key=new)
        )
    retired = [r for r in rows if r.key not in CATALOG_KEYS]
    for r in retired:
        await session.delete(r)
        rows.remove(r)
        changed = True
    if retired:
        await session.execute(
            sa_delete(PricingPlanFeature).where(
                PricingPlanFeature.feature_key.in_([r.key for r in retired])
            )
        )
    by_key = {r.key: r for r in rows}
    for entry in CATALOG:
        key = str(entry["key"])
        row = by_key.get(key)
        if row is None:
            row = PlanFeatureCatalog(**entry)
            session.add(row)
            rows.append(row)
            changed = True
            continue
        for field in ("label", "module", "audience", "limit_type", "display_order"):
            if getattr(row, field) != entry[field]:
                setattr(row, field, entry[field])
                changed = True
    if changed:
        await session.flush()
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


async def _init_stripe(session: AsyncSession) -> None:
    """Point the SDK at the credentials in force: admin-saved, else the environment."""
    from app.services import stripe_config_service

    keys = await stripe_config_service.effective_keys(session, get_settings())
    if not keys.secret_key:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    stripe.api_key = keys.secret_key


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


def _product_idempotency_key(plan: PricingPlan) -> str:
    """Stable per plan, except after a repair.

    Every plan keeps the key it has always had. Only a plan whose stale id we just
    cleared gets a new generation, because Stripe would otherwise replay the earlier
    response and hand back the very object the configured account cannot see.
    """
    version = plan.price_version or 1
    base = f"plan_product_{plan.id}"
    return base if version <= 1 else f"{base}_v{version}"


async def _visible_to_account(kind: str, object_id: str) -> bool:
    """Whether the configured account can actually see this object.

    Every Stripe id belongs to the account that created it. After a key change the
    ids we hold are still well-formed and still wrong — and a wrong id is not a
    *missing* one, which is the only thing ``_sync_to_stripe`` used to look for.

    A ``No such …`` answer is a fact about the object. Anything else (auth, network,
    rate limit) is a fact about the connection, so it is re-raised for the caller's
    handler to record — never treated as "the object is gone".
    """
    try:
        if kind == "product":
            await stripe.Product.retrieve_async(object_id)
        else:
            await stripe.Price.retrieve_async(object_id)
    except stripe.InvalidRequestError:
        return False
    return True


async def _drop_ids_the_account_cannot_see(session: AsyncSession, plan: PricingPlan) -> None:
    """Clear stale Stripe ids so the create below genuinely repairs the plan.

    Without this, a plan carrying another account's ids skipped both create branches
    and still fell through to ``status = active`` with ``sync_error`` cleared: Retry
    sync reported success on a plan no customer could buy, and erased the only clue.
    """
    if plan.stripe_product_id and not await _visible_to_account("product", plan.stripe_product_id):
        log.warning(
            "plan_stripe_product_unknown",
            plan_id=str(plan.id),
            stripe_product_id=plan.stripe_product_id,
        )
        # A price lives inside its product, so both are gone together.
        plan.stripe_product_id = None
        plan.stripe_price_id = None
    elif plan.stripe_price_id and not await _visible_to_account("price", plan.stripe_price_id):
        log.warning(
            "plan_stripe_price_unknown",
            plan_id=str(plan.id),
            stripe_price_id=plan.stripe_price_id,
        )
        plan.stripe_price_id = None
    else:
        return
    # The idempotency keys are derived from the plan id and price version, so the
    # replacement needs a new generation or Stripe may replay the previous response.
    plan.price_version = (plan.price_version or 1) + 1
    await session.flush()


async def _sync_to_stripe(
    session: AsyncSession, plan: PricingPlan, *, verify_existing: bool = False
) -> None:
    """Create whatever Stripe object is missing; never duplicates thanks to the keys.

    ``verify_existing`` additionally checks that the ids already stored are ones the
    configured account can see. It costs a round trip per id, so it is reserved for
    ``retry_sync`` — the admin pressing Retry is saying the plan is already wrong,
    which is the only moment the stored ids are worth doubting. On create and update
    we wrote them ourselves moments earlier.
    """
    if Decimal(str(plan.price_usd)) <= 0:
        plan.status = PlanStatus.active
        plan.sync_error = None
        plan.last_synced_at = datetime.now(UTC)
        return
    await _init_stripe(session)
    try:
        if verify_existing:
            await _drop_ids_the_account_cannot_see(session, plan)
        if not plan.stripe_product_id:
            product = await stripe.Product.create_async(
                name=plan.name,
                metadata=_metadata(plan),
                idempotency_key=_product_idempotency_key(plan),
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
    if data.is_default and Decimal(str(data.price_usd)) > 0:
        raise AppError("Only a free plan can be the default plan", code="default_plan_must_be_free")
    plan = PricingPlan(
        audience=audience,
        name=data.name,
        description=data.description,
        tagline=data.tagline,
        price_usd=Decimal(str(data.price_usd)).quantize(_CENT),
        billing_interval=BillingInterval(data.billing_interval),
        is_highlighted=data.is_highlighted,
        is_default=False,
        status=PlanStatus.draft,
        price_version=1,
        created_by=admin_id,
        features=features,
    )
    session.add(plan)
    await session.flush()
    if data.is_default:
        await _make_default(session, plan)
    await _sync_to_stripe(session, plan)
    session.add(plan)
    await session.flush()
    await session.refresh(plan)
    return plan


async def retry_sync(session: AsyncSession, plan_id: uuid.UUID, admin_id: uuid.UUID) -> PricingPlan:
    plan = await get(session, plan_id)
    if plan.status == PlanStatus.inactive:
        raise AppError("Activate the plan before syncing it", code="plan_inactive")
    await _sync_to_stripe(session, plan, verify_existing=True)
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
    if data.is_default is True:
        new_price = (
            Decimal(str(fields["price_usd"])) if fields.get("price_usd") is not None else None
        )
        if (new_price if new_price is not None else Decimal(str(plan.price_usd))) > 0:
            raise AppError(
                "Only a free plan can be the default plan", code="default_plan_must_be_free"
            )
        await _make_default(session, plan)
    elif data.is_default is False and plan.is_default:
        raise AppError("Mark another free plan as the default first", code="default_plan_in_use")
    plan.updated_by = admin_id
    await session.flush()

    if price_changed:
        if plan.is_default and Decimal(str(fields["price_usd"])) > 0:
            raise AppError(
                "The default plan must stay free; mark another plan as the default first",
                code="default_plan_in_use",
            )
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
            await _archive_price(session, old_price_id)
    elif text_changed and plan.stripe_product_id:
        await _init_stripe(session)
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


async def _make_default(session: AsyncSession, plan: PricingPlan) -> None:
    """Exactly one default per audience: the flag moves, it is never shared."""
    await session.execute(
        sa_update(PricingPlan)
        .where(PricingPlan.audience == plan.audience, PricingPlan.id != plan.id)
        .values(is_default=False)
    )
    plan.is_default = True
    session.add(plan)
    await session.flush()


async def _archive_price(session: AsyncSession, price_id: str) -> None:
    await _init_stripe(session)
    try:
        await stripe.Price.modify_async(price_id, active=False)
    except stripe.StripeError as exc:
        log.warning("price_archive_failed", price_id=price_id, error=str(exc)[:200])


async def set_active(
    session: AsyncSession, plan_id: uuid.UUID, active: bool, admin_id: uuid.UUID
) -> PricingPlan:
    """Deactivate archives the Stripe objects (never deletes); activate restores them."""
    plan = await get(session, plan_id)
    if not active and plan.is_default:
        # Everyone without a paid subscription sits on this plan; without it the
        # audience is locked out of every perk, not let into them.
        raise AppError(
            "The default free plan cannot be deactivated; mark another plan as the default first",
            code="default_plan_in_use",
        )
    if plan.stripe_product_id or plan.stripe_price_id:
        await _init_stripe(session)
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
    """The audience's designated default free plan, if the admin configured one.

    A $0 plan that is not the default is just a plan nobody is on (BUG12).
    """
    stmt = (
        select(PricingPlan)
        .where(
            PricingPlan.audience == PlanAudience(audience),
            PricingPlan.status == PlanStatus.active,
            PricingPlan.is_default.is_(True),
        )
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
        is_default=plan.is_default,
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
