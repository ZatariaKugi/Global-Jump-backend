from __future__ import annotations

from httpx import AsyncClient

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
USERS = "/api/v1/users"


async def _make_user(client: AsyncClient, email: str) -> None:
    resp = await client.post(REGISTER, json={"email": email, "password": "supersecret"})
    assert resp.status_code == 201, resp.text


async def test_list_users_is_paginated(client: AsyncClient, admin_token: str) -> None:
    for i in range(4):
        await _make_user(client, f"user{i}@example.com")
    # 4 seekers + 1 admin (created in fixture) = 5 total
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.get(f"{USERS}?page=1&page_size=2", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["success"] is True
    assert "timestamp" in body["meta"]
    assert len(body["data"]) == 2
    pg = body["meta"]["pagination"]
    assert pg["page"] == 1
    assert pg["page_size"] == 2
    assert pg["total"] == 5
    assert pg["pages"] == 3


async def test_list_users_last_page(client: AsyncClient, admin_token: str) -> None:
    for i in range(4):
        await _make_user(client, f"user{i}@example.com")
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.get(f"{USERS}?page=3&page_size=2", headers=headers)
    body = resp.json()
    pg = body["meta"]["pagination"]
    assert len(body["data"]) == 1
    assert pg["page"] == 3
    assert pg["pages"] == 3


async def test_list_users_requires_admin(client: AsyncClient) -> None:
    # A seeker token must not be able to list users.
    await _make_user(client, "cust@example.com")
    login = await client.post(
        LOGIN, data={"username": "cust@example.com", "password": "supersecret"}
    )
    token = login.json()["access_token"]
    resp = await client.get(USERS, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_page_size_over_max_rejected(client: AsyncClient, admin_token: str) -> None:
    resp = await client.get(
        f"{USERS}?page_size=1000", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 422
