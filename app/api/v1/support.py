"""Support / contact-us endpoint — authenticated users only."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.db.session import SessionDep
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.support_message import SupportMessageCreate, SupportMessageRead
from app.services import support_message_service

router = APIRouter(prefix="/support", tags=["support"])


@router.post(
    "",
    status_code=201,
    response_model=ResponseEnvelope[SupportMessageRead],
)
async def submit_support_message(
    data: SupportMessageCreate,
    # current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SupportMessageRead]:
    result = await support_message_service.create(
        session, data, settings
    )
    return ResponseEnvelope[SupportMessageRead](
        data=result,
        meta=Meta(request_id=request_id),
    )
