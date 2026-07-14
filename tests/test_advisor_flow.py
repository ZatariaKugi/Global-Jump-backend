"""Tests for advisor registration, verification workflow, and admin RBAC."""

from __future__ import annotations

from httpx import AsyncClient

REGISTER_ADVISOR = "/api/v1/auth/register/advisor"
LOGIN = "/api/v1/auth/login"
ADMIN_ADVISORS = "/api/v1/admin/advisors"

ADVISOR_CREDS = {
    "email": "jane@lawfirm.com",
    "password": "securepass1",
    "full_name": "Jane Advisor",
}


async def _register_advisor(client: AsyncClient) -> dict:
    resp = await client.post(REGISTER_ADVISOR, json=ADVISOR_CREDS)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_advisor_registration_returns_pending(client: AsyncClient) -> None:
    body = await _register_advisor(client)
    assert body["data"]["role"] == "advisor"
    assert body["data"]["verification_status"] == "pending"
    assert body["data"]["is_active"] is False


async def test_advisor_can_login_while_pending_for_onboarding(client: AsyncClient) -> None:
    """Pending advisors must be able to log in to complete profile + credential upload."""
    await _register_advisor(client)
    resp = await client.post(
        LOGIN, data={"username": ADVISOR_CREDS["email"], "password": ADVISOR_CREDS["password"]}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_advisor_can_login_after_approval(client: AsyncClient, admin_token: str) -> None:
    advisor_body = await _register_advisor(client)
    advisor_id = advisor_body["data"]["id"]

    # Admin approves
    resp = await client.patch(
        f"{ADMIN_ADVISORS}/{advisor_id}/verification",
        json={"status": "approved"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is True
    assert resp.json()["data"]["verification_status"] == "approved"

    # Now advisor can login
    resp = await client.post(
        LOGIN, data={"username": ADVISOR_CREDS["email"], "password": ADVISOR_CREDS["password"]}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_admin_can_list_advisors(client: AsyncClient, admin_token: str) -> None:
    await _register_advisor(client)
    resp = await client.get(ADMIN_ADVISORS, headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) >= 1
    assert body["meta"]["pagination"] is not None


async def test_admin_can_filter_advisors_by_status(client: AsyncClient, admin_token: str) -> None:
    await _register_advisor(client)
    resp = await client.get(
        f"{ADMIN_ADVISORS}?status=pending",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    for advisor in resp.json()["data"]:
        assert advisor["verification_status"] == "pending"


async def test_seeker_cannot_access_admin_endpoints(client: AsyncClient) -> None:
    from tests.test_auth import REGISTER

    await client.post(REGISTER, json={"email": "c@x.com", "password": "pass1234"})
    login = await client.post(LOGIN, data={"username": "c@x.com", "password": "pass1234"})
    token = login.json()["access_token"]

    resp = await client.get(ADMIN_ADVISORS, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_registration_role_cannot_be_escalated(client: AsyncClient) -> None:
    """Verify that passing role=admin in the registration body has no effect."""
    from tests.test_auth import REGISTER

    resp = await client.post(
        REGISTER,
        json={"email": "hacker@x.com", "password": "pass1234", "role": "admin"},
    )
    assert resp.status_code == 201
    # role is always seeker regardless of what was sent
    assert resp.json()["data"]["role"] == "seeker"
