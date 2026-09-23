"""Public Pre-Registration form endpoint (#309) — no auth required."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import RequestIdDep
from app.db.session import SessionDep
from app.schemas.pre_registration import PreRegistrationCreate, PreRegistrationRead
from app.schemas.response import Meta, ResponseEnvelope
from app.services import pre_registration_service

router = APIRouter(prefix="/pre-registrations", tags=["pre-registrations"])


@router.post("", status_code=201, response_model=ResponseEnvelope[PreRegistrationRead])
async def submit_pre_registration(
    data: PreRegistrationCreate,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[PreRegistrationRead]:
    row = await pre_registration_service.create(session, data)
    return ResponseEnvelope[PreRegistrationRead](
        data=PreRegistrationRead.model_validate(row), meta=Meta(request_id=request_id)
    )
