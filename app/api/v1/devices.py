"""FCM device-token registration — one endpoint for iOS, Android, and web."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, RequestIdDep
from app.db.session import SessionDep
from app.schemas.device import DeviceRead, DeviceRegister, DeviceUnregister
from app.schemas.response import Meta, ResponseEnvelope
from app.services import notification_service

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", status_code=201, response_model=ResponseEnvelope[DeviceRead])
async def register_device(
    data: DeviceRegister,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[DeviceRead]:
    """Register (or refresh) an FCM device token for the current user.

    Upsert by token: re-registering is safe, and a token previously owned by
    another account is reassigned to the caller.
    """
    device = await notification_service.register_device(
        session, current_user.id, data.token, data.platform
    )
    return ResponseEnvelope[DeviceRead](
        data=DeviceRead.model_validate(device), meta=Meta(request_id=request_id)
    )


@router.delete("", status_code=204)
async def unregister_device(
    data: DeviceUnregister,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    """Remove a device token (called on logout)."""
    await notification_service.unregister_device(session, current_user.id, data.token)
