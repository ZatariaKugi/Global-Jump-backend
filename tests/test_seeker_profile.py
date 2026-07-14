"""Tests for seeker profile endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _register_and_login(client: AsyncClient, email: str, password: str = "pass1234!") -> str:
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Test Seeker"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


@pytest.fixture
async def seeker_token(client: AsyncClient) -> str:
    return await _register_and_login(client, "seeker@test.com")


async def test_get_my_profile_creates_blank(client: AsyncClient, seeker_token: str) -> None:
    resp = await client.get(
        "/api/v1/users/me/profile",
        headers={"Authorization": f"Bearer {seeker_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["nationality"] is None
    assert data["countries_visited"] == []
    assert data["passport_number_masked"] is None


async def test_update_profile_basic_fields(client: AsyncClient, seeker_token: str) -> None:
    resp = await client.patch(
        "/api/v1/users/me/profile",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json={
            "nationality": "PK",
            "country_of_residence": "GB",
            "education_level": "bachelor",
            "employment_status": "employed",
            "email_notifications": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["nationality"] == "PK"
    assert data["country_of_residence"] == "GB"
    assert data["education_level"] == "bachelor"
    assert data["employment_status"] == "employed"
    assert data["email_notifications"] is False


async def test_update_profile_passport_encrypts(client: AsyncClient, seeker_token: str) -> None:
    resp = await client.patch(
        "/api/v1/users/me/profile",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json={"passport_number": "AB1234567", "passport_expiry": "2028-06-01"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Raw passport number never returned — only last 4 chars
    assert data["passport_number_masked"] == "4567"
    assert data["passport_expiry"] == "2028-06-01"


async def test_update_profile_travel_history(client: AsyncClient, seeker_token: str) -> None:
    resp = await client.patch(
        "/api/v1/users/me/profile",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json={
            "countries_visited": ["FR", "DE", "US"],
            "prior_visas": [{"country": "GB", "visa_type": "student", "year": 2020}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "FR" in data["countries_visited"]
    assert data["prior_visas"][0]["country"] == "GB"


async def test_advisor_cannot_access_seeker_profile(
    client: AsyncClient, advisor_token: str
) -> None:
    resp = await client.get(
        "/api/v1/users/me/profile",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 403


async def test_update_profile_onboarding_intent_fields(
    client: AsyncClient, seeker_token: str
) -> None:
    resp = await client.patch(
        "/api/v1/users/me/profile",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json={"intended_visa_type": "work", "intended_destination": "GB"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["intended_visa_type"] == "work"
    assert data["intended_destination"] == "GB"


async def test_profile_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/users/me/profile")
    assert resp.status_code == 401


async def test_onboarding_complete_wizard(client: AsyncClient, seeker_token: str) -> None:
    """Single POST at the final wizard step persists all onboarding data."""
    payload = {
        # Step 1 — visa intent
        "intended_visa_type": "work",
        # Step 2 — destination (full country name, resolved to ISO code server-side)
        "intended_destination": "Canada",
        # Step 3 — finance
        "annual_income_band": "50000-100000",
        # Step 4 — travel history (self-reported band, not actual country codes)
        "countries_visited": "Traveled to 1-2 countries",
        # Step 5 — AI assessment matching categories
        "matching_opportunities": ["visa_type", "interest", "finance", "travel_history"],
        # Steps 5-6 — AI matching (optional)
        "nationality": "PK",
        "education_level": "bachelor",
        "employment_status": "employed",
        "employer_name": "Acme Corp",
    }
    resp = await client.post(
        "/api/v1/users/me/onboarding",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["intended_visa_type"] == "work"
    assert data["intended_destination"] == "CA"
    assert data["annual_income_band"] == "50000-100000"
    # The band answer isn't a real country list — the profile's actual
    # countries_visited (set separately via PATCH /profile) stays empty.
    assert data["countries_visited"] == []
    assert data["nationality"] == "PK"
    assert data["education_level"] == "bachelor"
    assert data["employment_status"] == "employed"
    assert data["employer_name"] == "Acme Corp"
    # Step 5 AI suggestions — no OpenAI key in tests, so it degrades to an empty list
    assert data["ai_suggestions"] == []


async def test_onboarding_accepts_country_code_destination(
    client: AsyncClient, seeker_token: str
) -> None:
    """intended_destination also accepts a bare 2-letter code, case-insensitively."""
    resp = await client.post(
        "/api/v1/users/me/onboarding",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json={
            "intended_visa_type": "work",
            "intended_destination": "jp",
            "annual_income_band": "50000-100000",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["intended_destination"] == "JP"


async def test_onboarding_rejects_unrecognized_destination(
    client: AsyncClient, seeker_token: str
) -> None:
    resp = await client.post(
        "/api/v1/users/me/onboarding",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json={
            "intended_visa_type": "work",
            "intended_destination": "Narnia",
            "annual_income_band": "50000-100000",
        },
    )
    assert resp.status_code == 422


async def test_onboarding_requires_seeker_role(client: AsyncClient, advisor_token: str) -> None:
    resp = await client.post(
        "/api/v1/users/me/onboarding",
        headers={"Authorization": f"Bearer {advisor_token}"},
        json={
            "intended_visa_type": "work",
            "intended_destination": "CA",
            "annual_income_band": "50000-100000",
        },
    )
    assert resp.status_code == 403


async def test_onboarding_missing_required_fields(client: AsyncClient, seeker_token: str) -> None:
    resp = await client.post(
        "/api/v1/users/me/onboarding",
        headers={"Authorization": f"Bearer {seeker_token}"},
        json={"intended_visa_type": "work"},  # missing destination + income band
    )
    assert resp.status_code == 422
