from __future__ import annotations

from httpx import AsyncClient

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/users/me"

CREDS = {"email": "ada@example.com", "password": "supersecret", "full_name": "Ada"}


async def _register_and_login(client: AsyncClient) -> str:
    resp = await client.post(REGISTER, json=CREDS)
    assert resp.status_code == 201, resp.text
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


async def test_login_and_me_happy_path(client: AsyncClient) -> None:
    token = await _register_and_login(client)
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
