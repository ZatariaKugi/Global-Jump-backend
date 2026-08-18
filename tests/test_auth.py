from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.token import TokenPurpose, UserToken
from app.models.user import AuthProvider, User
from app.services import auth_service, user_service

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/users/me"
VERIFY_EMAIL = "/api/v1/auth/verify-email"
RESEND_VERIFICATION = "/api/v1/auth/resend-verification"
FORGOT_PASSWORD = "/api/v1/auth/forgot-password"
RESET_PASSWORD = "/api/v1/auth/reset-password"

CREDS = {"email": "ada@example.com", "password": "SuperSecret1!", "full_name": "Ada"}


async def _register_and_login(client: AsyncClient, engine) -> str:
    resp = await client.post(REGISTER, json=CREDS)
    assert resp.status_code == 201, resp.text
    _, raw_token = await _issue_verification_token(engine, CREDS["email"])
    resp = await client.post(VERIFY_EMAIL, json={"token": raw_token})
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        LOGIN, data={"username": CREDS["email"], "password": CREDS["password"]}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_register_returns_user(client: AsyncClient) -> None:
    resp = await client.post(REGISTER, json=CREDS)
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["email"] == CREDS["email"]
    assert "hashed_password" not in body["data"]  # never leak the hash
    assert body["meta"]["request_id"]  # envelope carries the correlation id


async def test_duplicate_registration_conflicts(client: AsyncClient) -> None:
    await client.post(REGISTER, json=CREDS)
    resp = await client.post(REGISTER, json=CREDS)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


async def test_login_and_me_happy_path(client: AsyncClient, engine) -> None:
    token = await _register_and_login(client, engine)
    resp = await client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["data"]["email"] == CREDS["email"]


async def test_me_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(ME)
    assert resp.status_code == 401


async def test_wrong_password_rejected(client: AsyncClient) -> None:
    await client.post(REGISTER, json=CREDS)
    resp = await client.post(LOGIN, data={"username": CREDS["email"], "password": "wrong-password"})
    assert resp.status_code == 401


async def _issue_verification_token(engine, email: str) -> tuple[str, str]:
    """Return (user_id, raw_token) for an unverified user."""
    settings = get_settings()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = await user_service.get_by_email(session, email)
        assert user is not None
        raw = await auth_service.create_email_verification_token(session, user, settings)
        await session.commit()
        return str(user.id), raw


async def _unused_verification_token_count(engine, user_id: str) -> int:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(UserToken.id)).where(
                UserToken.user_id == uuid.UUID(user_id),
                UserToken.purpose == TokenPurpose.email_verification,
                UserToken.used_at.is_(None),
            )
        )
        return int(result.scalar_one())


async def test_verify_email_consumes_token(client: AsyncClient, engine) -> None:
    creds = {"email": "verify1@test.com", "password": "SuperSecret1!", "full_name": "Verify One"}
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["is_email_verified"] is False

    _, raw_token = await _issue_verification_token(engine, creds["email"])

    resp = await client.post(VERIFY_EMAIL, json={"token": raw_token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_email_verified"] is True

    resp = await client.post(VERIFY_EMAIL, json={"token": raw_token})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "verify_token_expired"


async def test_resend_revokes_previous_verification_token(client: AsyncClient, engine) -> None:
    creds = {"email": "verify2@test.com", "password": "SuperSecret1!", "full_name": "Verify Two"}
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    _, first_token = await _issue_verification_token(engine, creds["email"])

    captured: dict[str, str] = {}

    async def _capture_token(_to: str, _name: str, raw_token: str, _settings: object) -> None:
        captured["token"] = raw_token

    with patch(
        "app.api.v1.auth.send_verification_email",
        new=AsyncMock(side_effect=_capture_token),
    ):
        resp = await client.post(RESEND_VERIFICATION, json={"email": creds["email"]})
    assert resp.status_code == 204, resp.text
    second_token = captured["token"]

    resp = await client.post(VERIFY_EMAIL, json={"token": first_token})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "verify_token_expired"

    resp = await client.post(VERIFY_EMAIL, json={"token": second_token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_email_verified"] is True


async def test_verify_email_rejects_already_verified_user(client: AsyncClient, engine) -> None:
    creds = {"email": "verify3@test.com", "password": "SuperSecret1!", "full_name": "Verify Three"}
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    _, raw_token = await _issue_verification_token(engine, creds["email"])

    resp = await client.post(VERIFY_EMAIL, json={"token": raw_token})
    assert resp.status_code == 200, resp.text

    settings = get_settings()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = await user_service.get_by_email(session, creds["email"])
        assert user is not None
        stale_token = await auth_service.create_email_verification_token(session, user, settings)
        await session.commit()

    resp = await client.post(VERIFY_EMAIL, json={"token": stale_token})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_verified"


async def test_resend_verification_rejects_already_verified_users(
    client: AsyncClient, engine
) -> None:
    creds = {
        "email": "verify4@test.com",
        "password": "SuperSecret1!",
        "full_name": "Verify Four",
    }
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    user_id, raw_token = await _issue_verification_token(engine, creds["email"])

    resp = await client.post(VERIFY_EMAIL, json={"token": raw_token})
    assert resp.status_code == 200, resp.text

    before = await _unused_verification_token_count(engine, user_id)
    resp = await client.post(RESEND_VERIFICATION, json={"email": creds["email"]})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "already_verified"
    after = await _unused_verification_token_count(engine, user_id)
    assert after == before


async def test_forgot_password_does_not_enumerate_emails(client: AsyncClient, engine) -> None:
    """Unknown, Google-only, and local accounts all return 204 (no mail for the first two)."""
    resp = await client.post(FORGOT_PASSWORD, json={"email": "nobody@example.com"})
    assert resp.status_code == 204, resp.text

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            User(
                email="google-user@example.com",
                full_name="Google User",
                hashed_password=None,
                auth_provider=AuthProvider.google,
                google_sub="google-sub-1",
            )
        )
        await session.commit()

    with patch(
        "app.api.v1.auth.send_password_reset_email",
        new=AsyncMock(),
    ) as send_mail:
        resp = await client.post(FORGOT_PASSWORD, json={"email": "google-user@example.com"})
        assert resp.status_code == 204, resp.text
        send_mail.assert_not_called()

    creds = {"email": "local@example.com", "password": "SuperSecret1!", "full_name": "Local"}
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    with patch(
        "app.api.v1.auth.send_password_reset_email",
        new=AsyncMock(),
    ) as send_mail:
        resp = await client.post(FORGOT_PASSWORD, json={"email": creds["email"]})
        assert resp.status_code == 204, resp.text
        send_mail.assert_called_once()


async def test_verify_email_rejects_stale_token_after_version_bump(
    client: AsyncClient, engine
) -> None:
    creds = {"email": "verify-tv@test.com", "password": "SuperSecret1!", "full_name": "TV"}
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    _, raw_token = await _issue_verification_token(engine, creds["email"])

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = await user_service.get_by_email(session, creds["email"])
        assert user is not None
        user.token_version = user.token_version + 1
        session.add(user)
        await session.commit()

    resp = await client.post(VERIFY_EMAIL, json={"token": raw_token})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "verify_token_expired"


async def test_reset_password_invalidates_existing_access_token(
    client: AsyncClient, engine
) -> None:
    creds = {"email": "reset-jwt@test.com", "password": "SuperSecret1!", "full_name": "Reset JWT"}
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    _, verify_token = await _issue_verification_token(engine, creds["email"])
    resp = await client.post(VERIFY_EMAIL, json={"token": verify_token})
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        LOGIN, data={"username": creds["email"], "password": creds["password"]}
    )
    assert resp.status_code == 200, resp.text
    old_access = resp.json()["access_token"]

    settings = get_settings()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        user = await user_service.get_by_email(session, creds["email"])
        assert user is not None
        raw_reset = await auth_service.create_password_reset_token_for_user(session, user, settings)
        await session.commit()

    resp = await client.post(
        RESET_PASSWORD, json={"token": raw_reset, "new_password": "BrandNew1!"}
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(ME, headers={"Authorization": f"Bearer {old_access}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_in_app_password_change_invalidates_existing_access_token(
    client: AsyncClient, engine
) -> None:
    creds = {"email": "patch-pw@test.com", "password": "SuperSecret1!", "full_name": "Patch PW"}
    resp = await client.post(REGISTER, json=creds)
    assert resp.status_code == 201, resp.text
    _, verify_token = await _issue_verification_token(engine, creds["email"])
    resp = await client.post(VERIFY_EMAIL, json={"token": verify_token})
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        LOGIN, data={"username": creds["email"], "password": creds["password"]}
    )
    assert resp.status_code == 200, resp.text
    old_access = resp.json()["access_token"]

    resp = await client.patch(
        ME,
        json={"password": "BrandNew1!"},
        headers={"Authorization": f"Bearer {old_access}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(ME, headers={"Authorization": f"Bearer {old_access}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
