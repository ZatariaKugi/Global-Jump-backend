"""Self-service subscriptions and entitlements (EPIC 04, PAY-110 / PAY-111)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.exceptions import NotFoundError
from app.db.session import SessionDep
from app.models.subscription import SubscriptionInvoice
from app.schemas.payment import CheckoutResponse
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.subscription import (
    ChangePlanPreviewRead,
    EntitlementsRead,
    PortalLinkRead,
    SubscriptionCancel,
    SubscriptionChangePlan,
    SubscriptionCheckoutCreate,
    SubscriptionInvoiceRead,
    SubscriptionRead,
)
from app.services import entitlement_service, subscription_service

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])
entitlements_router = APIRouter(prefix="/entitlements", tags=["subscriptions"])


@router.post("/checkout", response_model=ResponseEnvelope[CheckoutResponse], status_code=201)
async def create_subscription_checkout(
    data: SubscriptionCheckoutCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[CheckoutResponse]:
    """Stripe hosted Checkout (mode=subscription) for one active plan of the caller's audience."""
    result = await subscription_service.create_checkout(
        session, current_user, data.plan_id, settings
    )
    return ResponseEnvelope[CheckoutResponse](data=result, meta=Meta(request_id=request_id))


@router.post("/me/choose-free", response_model=ResponseEnvelope[dict[str, bool]])
async def choose_free_plan(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[dict[str, bool]]:
    """The first-time plans page: record that the user chose the free plan.

    Creates no subscription row and is safe to call repeatedly. Clears
    ``plan_choice_required`` on ``GET /users/me``.
    """
    await subscription_service.choose_free(session, current_user)
    return ResponseEnvelope[dict[str, bool]](
        data={"acknowledged": True}, meta=Meta(request_id=request_id)
    )


@router.get("/me", response_model=ResponseEnvelope[SubscriptionRead | None])
async def get_my_subscription(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SubscriptionRead | None]:
    # Reconciles from Stripe when no local row exists, so a webhook that never
    # arrived does not leave a paying user showing as free forever.
    sub = await subscription_service.get_current_for(session, current_user, settings)
    data = await subscription_service.read(session, sub) if sub else None
    return ResponseEnvelope[SubscriptionRead | None](data=data, meta=Meta(request_id=request_id))


@router.post("/me/cancel", response_model=ResponseEnvelope[SubscriptionRead])
async def cancel_my_subscription(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    data: SubscriptionCancel | None = None,
) -> ResponseEnvelope[SubscriptionRead]:
    """Schedule the move to the free plan at the period end (no immediate cancel)."""
    _ = data
    sub = await subscription_service.cancel(session, current_user, settings)
    return ResponseEnvelope[SubscriptionRead](
        data=await subscription_service.read(session, sub), meta=Meta(request_id=request_id)
    )


@router.post("/me/portal", response_model=ResponseEnvelope[PortalLinkRead])
async def open_billing_portal(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[PortalLinkRead]:
    """Stripe Billing Portal: card update, invoices, cancellation."""
    url = await subscription_service.portal_url(session, current_user, settings)
    return ResponseEnvelope[PortalLinkRead](
        data=PortalLinkRead(portal_url=url), meta=Meta(request_id=request_id)
    )


@router.post("/me/change-plan", response_model=ResponseEnvelope[SubscriptionRead])
async def change_my_plan(
    data: SubscriptionChangePlan,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SubscriptionRead]:
    sub = await subscription_service.change_plan(session, current_user, data.plan_id, settings)
    return ResponseEnvelope[SubscriptionRead](
        data=await subscription_service.read(session, sub), meta=Meta(request_id=request_id)
    )


@router.get("/me/change-plan/preview", response_model=ResponseEnvelope[ChangePlanPreviewRead])
async def preview_plan_change(
    plan_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ChangePlanPreviewRead]:
    data = await subscription_service.preview_change(session, current_user, plan_id, settings)
    return ResponseEnvelope[ChangePlanPreviewRead](data=data, meta=Meta(request_id=request_id))


@router.get("/me/invoices", response_model=ResponseEnvelope[list[SubscriptionInvoiceRead]])
async def list_my_invoices(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[SubscriptionInvoiceRead]]:
    sub = await subscription_service.get_current_for(session, current_user, settings)
    if sub is None:
        return ResponseEnvelope[list[SubscriptionInvoiceRead]](
            data=[], meta=page_meta(params, 0, request_id)
        )
    # Rebuilds the history from Stripe when we hold none, so a webhook that never
    # landed does not leave the subscriber staring at an empty page.
    await subscription_service.reconcile_invoices(session, sub, settings)
    rows, total = await paginate(session, subscription_service.invoices_stmt(sub.id), params)
    return ResponseEnvelope[list[SubscriptionInvoiceRead]](
        data=[subscription_service.invoice_read(r) for r in rows],
        meta=page_meta(params, total, request_id),
    )


@router.get("/me/invoices/{invoice_id}", response_model=ResponseEnvelope[SubscriptionInvoiceRead])
async def get_my_invoice(
    invoice_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SubscriptionInvoiceRead]:
    inv = await session.get(SubscriptionInvoice, invoice_id)
    sub = await subscription_service.get_current(session, current_user.id)
    if inv is None or sub is None or inv.subscription_id != sub.id:
        raise NotFoundError("Invoice not found")
    return ResponseEnvelope[SubscriptionInvoiceRead](
        data=subscription_service.invoice_read(inv), meta=Meta(request_id=request_id)
    )


@entitlements_router.get("/me", response_model=ResponseEnvelope[EntitlementsRead])
async def get_my_entitlements(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[EntitlementsRead]:
    """Plan features with usage, the map every gated screen reads."""
    data = await entitlement_service.get_entitlements(session, current_user)
    return ResponseEnvelope[EntitlementsRead](data=data, meta=Meta(request_id=request_id))
