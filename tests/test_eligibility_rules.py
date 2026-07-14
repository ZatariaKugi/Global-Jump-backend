"""Eligibility rules admin CRUD tests (PRD §3.4 AI Engine Management)."""

from __future__ import annotations

from httpx import AsyncClient

RULES = "/api/v1/admin/eligibility-rules"


async def _seeker_token(client: AsyncClient, email: str = "cust@test.com") -> str:
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "custpass123", "full_name": "Cust"},
    )
    resp = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "custpass123"}
    )
    return str(resp.json()["access_token"])


async def test_admin_can_create_list_update_delete_rule(
    client: AsyncClient, admin_token: str
) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.post(
        RULES,
        json={
            "name": "Education",
            "description": "Highest education level attained",
            "country_code": "gb",
            "visa_type": "Work",
            "points": 25,
            "weightage_pct": 25,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    rule = resp.json()["data"]
    assert rule["country_code"] == "GB"
    assert rule["visa_type"] == "work"
    assert rule["is_active"] is True
    rule_id = rule["id"]

    resp = await client.get(RULES, headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.patch(
        f"{RULES}/{rule_id}", json={"points": 30, "is_active": False}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["points"] == 30
    assert data["is_active"] is False

    resp = await client.delete(f"{RULES}/{rule_id}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(RULES, headers=headers)
    assert resp.json()["data"] == []


async def test_rules_scoped_by_country_and_visa_type(client: AsyncClient, admin_token: str) -> None:
    headers = {"Authorization": f"Bearer {admin_token}"}

    await client.post(
        RULES,
        json={
            "name": "GB rule",
            "country_code": "GB",
            "visa_type": "work",
            "points": 10,
            "weightage_pct": 10,
        },
        headers=headers,
    )
    await client.post(
        RULES,
        json={"name": "Global rule", "points": 20, "weightage_pct": 20},
        headers=headers,
    )

    resp = await client.get(f"{RULES}?country=GB&visa_type=work", headers=headers)
    assert resp.status_code == 200, resp.text
    names = {r["name"] for r in resp.json()["data"]}
    assert names == {"GB rule"}

    resp = await client.get(RULES, headers=headers)
    names = {r["name"] for r in resp.json()["data"]}
    assert names == {"GB rule", "Global rule"}


async def test_non_admin_forbidden_from_eligibility_rules(client: AsyncClient) -> None:
    token = await _seeker_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(RULES, headers=headers)
    assert resp.status_code == 403

    resp = await client.post(
        RULES,
        json={"name": "x", "points": 1, "weightage_pct": 1},
        headers=headers,
    )
    assert resp.status_code == 403
