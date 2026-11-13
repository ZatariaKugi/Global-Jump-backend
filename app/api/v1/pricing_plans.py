"""Public pricing plans (EPIC 04, PAY-112): what a seeker or advisor may buy."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import OptionalPrincipal, RequestIdDep
from app.db.session import SessionDep
from app.schemas.pricing_plan import PricingPlanPublicRead
from app.schemas.response import Meta, ResponseEnvelope
from app.services import pricing_plan_service

router = APIRouter(prefix="/pricing-plans", tags=["pricing-plans"])


@router.get("", response_model=ResponseEnvelope[list[PricingPlanPublicRead]])
async def list_pricing_plans(
    principal: OptionalPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
    audience: Annotated[Literal["seeker", "advisor"] | None, Query()] = None,
) -> ResponseEnvelope[list[PricingPlanPublicRead]]:
    """Active plans for one audience (defaults to the caller's role, else seeker).

    Public: the landing page shows the admin's plans to visitors (QA BUG10).
    No Stripe ids.
    """
    role = principal.role if principal is not None else None
    resolved = audience or (role if role in ("seeker", "advisor") else "seeker")
    plans = await pricing_plan_service.list_public(session, resolved)
    return ResponseEnvelope[list[PricingPlanPublicRead]](
        data=[pricing_plan_service.public_read(p) for p in plans],
        meta=Meta(request_id=request_id),
    )
