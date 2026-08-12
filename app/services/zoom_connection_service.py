"""Persist and manage Advisor Zoom connections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import decrypt_field, encrypt_field
from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger
from app.models.advisor_profile import AdvisorProfile
from app.models.zoom_connection import ZoomConnection, ZoomConnectionStatus
from app.schemas.zoom import ZoomStatusRead
from app.services import zoom_oauth_service

logger = get_logger(__name__)


async def get_by_advisor(
    session: AsyncSession, advisor_id: uuid.UUID
) -> ZoomConnection | None:
    result = await session.execute(
        select(ZoomConnection).where(
            ZoomConnection.advisor_id == advisor_id,
            ZoomConnection.is_archived.is_(False),
        )
    )
    return result.scalar_one_or_none()


async def get_active_by_advisor(
    session: AsyncSession, advisor_id: uuid.UUID
) -> ZoomConnection | None:
    connection = await get_by_advisor(session, advisor_id)
    if connection is None or connection.status != ZoomConnectionStatus.connected:
        return None
    return connection


def build_status(connection: ZoomConnection | None) -> ZoomStatusRead:
    if connection is None:
        return ZoomStatusRead(connected=False, zoom_email=None, connected_at=None, status=None)
    if connection.status != ZoomConnectionStatus.connected:
        return ZoomStatusRead(
            connected=False,
            zoom_email=connection.zoom_email,
            connected_at=connection.connected_at or connection.created_at,
            status=connection.status.value,
        )
    return ZoomStatusRead(
        connected=True,
        zoom_email=connection.zoom_email,
        connected_at=connection.connected_at or connection.created_at,
        status=connection.status.value,
    )


def needs_zoom_connect(connection: ZoomConnection | None) -> bool:
    """True when FE should show the Zoom connect banner."""
    return connection is None or connection.status != ZoomConnectionStatus.connected


def needs_stripe_connect(profile: AdvisorProfile) -> bool:
    """True until Stripe Connect can take charges and pay out."""
    account_id = profile.stripe_account_id
    return not (account_id and profile.stripe_charges_enabled and profile.stripe_payouts_enabled)


def sync_stripe_connect_flag(profile: AdvisorProfile) -> None:
    profile.needs_stripe_connect = needs_stripe_connect(profile)


def sync_zoom_connect_flag(profile: AdvisorProfile, connection: ZoomConnection | None) -> None:
    profile.needs_zoom_connect = needs_zoom_connect(connection)


async def _get_profile_for_advisor(
    session: AsyncSession, advisor_id: uuid.UUID
) -> AdvisorProfile:
    result = await session.execute(
        select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise NotFoundError("Advisor profile not found")
    return profile


async def upsert_from_oauth(
    session: AsyncSession,
    *,
    advisor_id: uuid.UUID,
    tokens: zoom_oauth_service.ZoomTokenSet,
    user: zoom_oauth_service.ZoomUserInfo,
    settings: Settings,
) -> ZoomConnection:
    """Create or replace the advisor's Zoom connection with fresh encrypted tokens."""
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)
    access_enc = encrypt_field(tokens.access_token, settings)
    refresh_enc = encrypt_field(tokens.refresh_token, settings)

    existing = await get_by_advisor(session, advisor_id)
    now = datetime.now(UTC)
    if existing is None:
        connection = ZoomConnection(
            advisor_id=advisor_id,
            zoom_user_id=user.zoom_user_id,
            zoom_account_id=user.zoom_account_id,
            zoom_email=user.zoom_email,
            access_token_encrypted=access_enc,
            refresh_token_encrypted=refresh_enc,
            access_token_expires_at=expires_at,
            scopes=tokens.scope,
            status=ZoomConnectionStatus.connected,
            connected_at=now,
            created_by=advisor_id,
            updated_by=advisor_id,
        )
        session.add(connection)
    else:
        connection = existing
        connection.zoom_user_id = user.zoom_user_id
        connection.zoom_account_id = user.zoom_account_id
        connection.zoom_email = user.zoom_email
        connection.access_token_encrypted = access_enc
        connection.refresh_token_encrypted = refresh_enc
        connection.access_token_expires_at = expires_at
        connection.scopes = tokens.scope
        connection.status = ZoomConnectionStatus.connected
        connection.connected_at = now
        connection.is_archived = False
        connection.updated_by = advisor_id
        session.add(connection)

    await session.flush()
    await session.refresh(connection)
    profile = await _get_profile_for_advisor(session, advisor_id)
    sync_zoom_connect_flag(profile, connection)
    await session.flush()
    logger.info("zoom_connection_saved", advisor_id=str(advisor_id))
    return connection


async def _persist_refreshed_tokens(
    session: AsyncSession,
    connection: ZoomConnection,
    tokens: zoom_oauth_service.ZoomTokenSet,
    settings: Settings,
) -> ZoomConnection:
    connection.access_token_encrypted = encrypt_field(tokens.access_token, settings)
    connection.refresh_token_encrypted = encrypt_field(tokens.refresh_token, settings)
    connection.access_token_expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)
    if tokens.scope:
        connection.scopes = tokens.scope
    connection.status = ZoomConnectionStatus.connected
    connection.updated_by = connection.advisor_id
    session.add(connection)
    await session.flush()
    await session.refresh(connection)
    return connection


async def mark_revoked(session: AsyncSession, connection: ZoomConnection) -> ZoomConnection:
    """Mark the connection revoked after Zoom rejects a refresh (invalid grant)."""
    connection.status = ZoomConnectionStatus.revoked
    connection.updated_by = connection.advisor_id
    session.add(connection)
    await session.flush()
    await session.refresh(connection)
    profile = await _get_profile_for_advisor(session, connection.advisor_id)
    sync_zoom_connect_flag(profile, connection)
    await session.flush()
    return connection


async def get_valid_zoom_access_token(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    settings: Settings,
) -> str:
    """Return a usable Zoom access token, refreshing when near expiry.

    If Zoom rejects the refresh (revoked app / invalid grant), the connection is
    marked ``revoked`` so the advisor must reconnect.
    """
    connection = await get_active_by_advisor(session, advisor_id)
    if connection is None:
        raise NotFoundError("Zoom is not connected")

    if not zoom_oauth_service.token_needs_refresh(connection.access_token_expires_at):
        return decrypt_field(connection.access_token_encrypted, settings)

    refresh_token = decrypt_field(connection.refresh_token_encrypted, settings)
    try:
        tokens = await zoom_oauth_service.refresh_access_token(refresh_token, settings)
    except AppError:
        await mark_revoked(session, connection)
        raise

    connection = await _persist_refreshed_tokens(session, connection, tokens, settings)
    return decrypt_field(connection.access_token_encrypted, settings)


async def disconnect(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    settings: Settings,
) -> None:
    """Revoke at Zoom (best-effort) and remove the local connection row."""
    connection = await get_by_advisor(session, advisor_id)
    if connection is None:
        raise NotFoundError("Zoom is not connected")

    # Prefer revoking the refresh token so Zoom invalidates the grant.
    try:
        refresh = decrypt_field(connection.refresh_token_encrypted, settings)
        await zoom_oauth_service.revoke_token(refresh, settings)
    except Exception:
        logger.warning("zoom_local_token_decrypt_or_revoke_failed", advisor_id=str(advisor_id))

    await session.delete(connection)
    await session.flush()
    profile = await _get_profile_for_advisor(session, advisor_id)
    sync_zoom_connect_flag(profile, None)
    await session.flush()
    logger.info("zoom_connection_deleted", advisor_id=str(advisor_id))
