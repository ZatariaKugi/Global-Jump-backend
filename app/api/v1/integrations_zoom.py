"""Advisor Zoom OAuth integration — all routes tagged ``integrations-zoom``."""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Query, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep, require_role
from app.core.config import Environment
from app.core.exceptions import AppError, AuthenticationError
from app.core.logging import get_logger
from app.db.session import SessionDep
from app.models.user import UserRole
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.zoom import ZoomConnectRead, ZoomStatusRead
from app.services import zoom_connection_service, zoom_oauth_service

# Public callback: /integrations/zoom/callback
router = APIRouter(prefix="/integrations/zoom", tags=["integrations-zoom"])

# Advisor-authenticated routes: /advisors/me/integrations/zoom*
advisor_router = APIRouter(prefix="/advisors", tags=["integrations-zoom"])

OAUTH_STATE_COOKIE = "gj_zoom_oauth_state"
_OAUTH_STATE_COOKIE_MAX_AGE = 600

logger = get_logger(__name__)


def set_zoom_oauth_state_cookie(response: Response, nonce: str, *, secure: bool) -> None:
    """Attach CSRF nonce cookie used by the Zoom callback."""
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=nonce,
        max_age=_OAUTH_STATE_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_zoom_oauth_state_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")


@advisor_router.get(
    "/me/integrations/zoom",
    response_model=ResponseEnvelope[ZoomStatusRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def get_my_zoom_status(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ZoomStatusRead]:
    """Zoom connection status for profile / integrations UI (never returns tokens)."""
    connection = await zoom_connection_service.get_by_advisor(session, current_user.id)
    return ResponseEnvelope[ZoomStatusRead](
        data=zoom_connection_service.build_status(connection),
        meta=Meta(request_id=request_id),
    )


@advisor_router.post(
    "/me/integrations/zoom/connect",
    response_model=ResponseEnvelope[ZoomConnectRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def connect_my_zoom(
    current_user: CurrentUser,
    settings: SettingsDep,
    request_id: RequestIdDep,
    response: Response,
) -> ResponseEnvelope[ZoomConnectRead]:
    """Start Zoom OAuth; returns ``authorize_url`` for the FE to navigate to.

    FE should ``window.location = data.authorize_url``. A CSRF cookie is set when
    possible, but the signed ``state`` query param alone is enough for the
    callback — cross-origin SPAs do not need ``withCredentials``.
    """
    zoom_oauth_service.require_zoom_configured(settings)
    state, nonce = zoom_oauth_service.sign_state(current_user.id, settings)
    url = zoom_oauth_service.build_authorization_url(settings, state)
    # Best-effort cookie (helps same-site flows). Not required for callback.
    set_zoom_oauth_state_cookie(
        response,
        nonce,
        secure=Environment.local != settings.ENVIRONMENT,
    )
    return ResponseEnvelope[ZoomConnectRead](
        data=ZoomConnectRead(authorize_url=url),
        meta=Meta(request_id=request_id),
    )


@advisor_router.delete(
    "/me/integrations/zoom",
    status_code=204,
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def disconnect_my_zoom(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """Disconnect Zoom: revoke grant (best-effort) and delete stored tokens."""
    zoom_oauth_service.require_zoom_configured(settings)
    await zoom_connection_service.disconnect(session, current_user.id, settings)


@router.get("/callback")
async def zoom_callback(
    session: SessionDep,
    settings: SettingsDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    oauth_state: Annotated[str | None, Cookie(alias=OAUTH_STATE_COOKIE)] = None,
) -> RedirectResponse:
    """Zoom redirects here after consent. Exchanges code, stores connection, returns to FE.

    Success → ``{FRONTEND_URL}/advisor/profile?zoom=connected``
    """

    def redirect(**query: str) -> RedirectResponse:
        response = RedirectResponse(
            zoom_oauth_service.frontend_return_url(settings, **query),
            status_code=status.HTTP_302_FOUND,
        )
        clear_zoom_oauth_state_cookie(response)
        return response

    if not settings.zoom_oauth_enabled:
        return redirect(zoom="error", reason="not_configured")

    if error:
        logger.info(
            "zoom_oauth_error",
            error=error,
            error_description=error_description,
        )
        reason = "cancelled" if error == "access_denied" else "oauth_error"
        return redirect(zoom="error", reason=reason)

    if not code or not state:
        return redirect(zoom="error", reason="invalid_request")

    try:
        advisor_id = zoom_oauth_service.verify_state(state, oauth_state, settings)
        tokens = await zoom_oauth_service.exchange_code(code, settings)
        zoom_user = await zoom_oauth_service.fetch_zoom_user(tokens.access_token)
        await zoom_connection_service.upsert_from_oauth(
            session,
            advisor_id=advisor_id,
            tokens=tokens,
            user=zoom_user,
            settings=settings,
        )
    except AuthenticationError:
        return redirect(zoom="error", reason="invalid_state")
    except AppError as exc:
        return redirect(zoom="error", reason=exc.code)
    except Exception:
        logger.exception("zoom_callback_unexpected_error")
        return redirect(zoom="error", reason="unable_to_connect")

    return redirect(zoom="connected")
