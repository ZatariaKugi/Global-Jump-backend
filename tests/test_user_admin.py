"""Admin "User Management" tests: broad account directory, verify, reset-password trigger."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.token import TokenPurpose, UserToken
from app.models.user import UserRole, VerificationStatus
from tests.test_analytics import _seed_user

USERS = "/api/v1/admin/users"


async def _seeker_token(client: AsyncClient, email: str = "seeker@test.com") -> str:
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "custpass123", "full_name": "Test Seeker"},
    )
    resp = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "custpass123"}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


# ── List / filter ─────────────────────────────────────────────────────────────


async def test_list_users_excludes_admins_on_empty_data(
    client: AsyncClient, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(USERS, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


async def test_list_users_role_filter(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    await _seed_user(engine, "list-seeker@test.com", "List Seeker", UserRole.seeker)
    await _seed_user(
        engine,
        "list-advisor@test.com",
        "List Advisor",
        UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
    )

    resp = await client.get(USERS, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    names = {row["full_name"] for row in resp.json()["data"]}
    assert names == {"List Seeker", "List Advisor"}

    resp = await client.get(f"{USERS}?role=advisor", headers=admin_headers)
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["full_name"] == "List Advisor"


# ── Verify account ───────────────────────────────────────────────────────────


async def test_verify_account(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_id = await _seed_user(engine, "to-verify@test.com", "To Verify", UserRole.seeker)

    resp = await client.get(f"{USERS}/{user_id}/profile", headers=admin_headers)
    assert resp.json()["data"]["status"] == "unverified"

    resp = await client.post(f"{USERS}/{user_id}/verify", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "verified"

    resp = await client.get(f"{USERS}/{user_id}/profile", headers=admin_headers)
    assert resp.json()["data"]["status"] == "verified"


# ── Reset password trigger ───────────────────────────────────────────────────


async def test_trigger_password_reset(client: AsyncClient, admin_token: str, engine) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_id = await _seed_user(engine, "to-reset@test.com", "To Reset", UserRole.seeker)

    resp = await client.post(f"{USERS}/{user_id}/reset-password", headers=admin_headers)
    assert resp.status_code == 204, resp.text

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        token_row = (
            await session.execute(
                select(UserToken).where(
                    UserToken.user_id == user_id, UserToken.purpose == TokenPurpose.password_reset
                )
            )
        ).scalar_one_or_none()
        assert token_row is not None


# ── Auth ──────────────────────────────────────────────────────────────────────


async def test_non_admin_forbidden(client: AsyncClient, admin_token: str, engine) -> None:
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    other_id = await _seed_user(engine, "other@test.com", "Other", UserRole.seeker)

    assert (await client.get(USERS, headers=headers)).status_code == 403
    assert (await client.get(f"{USERS}/{other_id}/profile", headers=headers)).status_code == 403
    assert (await client.post(f"{USERS}/{other_id}/verify", headers=headers)).status_code == 403
    assert (
        await client.post(f"{USERS}/{other_id}/reset-password", headers=headers)
    ).status_code == 403
