"""Public pricing plans (EPIC 04, PAY-112): what a seeker or advisor may buy."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, RequestIdDep
from app.db.session import SessionDep
from app.schemas.pricing_plan import PricingPlanPublicRead
from app.schemas.response import Meta, ResponseEnvelope
from app.services import pricing_plan_service

router = APIRouter(prefix="/pricing-plans", tags=["pricing-plans"])


@router.get("", response_model=ResponseEnvelope[list[PricingPlanPublicRead]])
async def list_pricing_plans(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    audience: Annotated[Literal["seeker", "advisor"] | None, Query()] = None,
) -> ResponseEnvelope[list[PricingPlanPublicRead]]:
    """Active plans for one audience (defaults to the caller's role). No Stripe ids."""
    resolved = audience or (
        current_user.role.value if current_user.role.value in ("seeker", "advisor") else "seeker"
    )
    plans = await pricing_plan_service.list_public(session, resolved)
    return ResponseEnvelope[list[PricingPlanPublicRead]](
        data=[pricing_plan_service.public_read(p) for p in plans],
        meta=Meta(request_id=request_id),
    )
