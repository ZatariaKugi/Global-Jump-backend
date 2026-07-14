"""Tests for advisor profile endpoints."""

from __future__ import annotations

import io

from httpx import AsyncClient


async def test_get_own_advisor_profile_creates_blank(
    client: AsyncClient, advisor_token: str
) -> None:
    resp = await client.get(
        "/api/v1/advisors/me/profile",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["bio"] is None
    assert data["visa_specializations"] == []
    assert data["services"] == []


async def test_update_advisor_profile(client: AsyncClient, advisor_token: str) -> None:
    resp = await client.patch(
        "/api/v1/advisors/me/profile",
        headers={"Authorization": f"Bearer {advisor_token}"},
        json={
            "title": "Immigration Lawyer",
            "bio": "10 years helping clients navigate visa applications.",
            "years_of_experience": 10,
            "visa_specializations": ["work", "student"],
            "country_expertise": ["GB", "CA", "AU"],
            "languages": [{"language": "English", "proficiency": "native"}],
            "services": [
                {
                    "service_type": "full_consultation",
                    "duration_minutes": 60,
                    "price_usd": 150.0,
                }
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["title"] == "Immigration Lawyer"
    assert data["years_of_experience"] == 10
    assert "work" in data["visa_specializations"]
    assert data["services"][0]["price_usd"] == 150.0


async def test_get_public_advisor_profile(client: AsyncClient, advisor_token: str) -> None:
    me_resp = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    advisor_id = me_resp.json()["data"]["id"]

    await client.patch(
        "/api/v1/advisors/me/profile",
        headers={"Authorization": f"Bearer {advisor_token}"},
        json={"bio": "Expert advisor.", "title": "Visa Specialist"},
    )

    resp = await client.get(
        f"/api/v1/advisors/{advisor_id}",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["bio"] == "Expert advisor."
    assert data["full_name"] == "Test Advisor"


async def test_list_advisors(client: AsyncClient, advisor_token: str) -> None:
    resp = await client.get(
        "/api/v1/advisors",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"], list)
    assert body["meta"]["pagination"]["total"] >= 1


async def test_seeker_cannot_access_advisor_me_profile(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "cust2@test.com", "password": "pass1234!", "full_name": "Cust"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "cust2@test.com", "password": "pass1234!"},
    )
    token = login.json()["access_token"]

    resp = await client.get(
        "/api/v1/advisors/me/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_get_nonexistent_advisor_returns_404(client: AsyncClient, advisor_token: str) -> None:
    import uuid

    resp = await client.get(
        f"/api/v1/advisors/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 404


async def test_advisor_onboarding_complete_wizard(client: AsyncClient, advisor_token: str) -> None:
    """Single POST at the final wizard step persists all advisor onboarding data."""
    payload = {
        # Step 1 — services
        "services": [{"service_type": "consultation", "duration_minutes": 60, "price_usd": 120.0}],
        # Steps 2-4 — expertise
        "visa_specializations": ["work", "student"],
        "country_expertise": ["GB", "CA"],
        # Step 5 — base location
        "base_country": "GB",
        # Step 6 — professional profile
        "title": "Immigration Consultant",
        "bio": "10 years helping clients worldwide.",
        "years_of_experience": 10,
    }
    resp = await client.post(
        "/api/v1/advisors/me/onboarding",
        headers={"Authorization": f"Bearer {advisor_token}"},
        json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["title"] == "Immigration Consultant"
    assert data["bio"] == "10 years helping clients worldwide."
    assert data["years_of_experience"] == 10
    assert "work" in data["visa_specializations"]
    assert "student" in data["visa_specializations"]
    # base_country GB prepended, then CA
    assert data["country_expertise"][0] == "GB"
    assert "CA" in data["country_expertise"]
    assert data["services"][0]["price_usd"] == 120.0
    # languages is no longer part of the onboarding flow — left empty
    assert data["languages"] == []
    # slug generated on first submit
    assert data["public_profile_slug"] is not None


async def test_advisor_onboarding_document_upload(client: AsyncClient, advisor_token: str) -> None:
    """Global upload with category=credential returns a file_key scoped to the advisor."""
    file_content = b"%PDF-1.4 fake pdf content"
    resp = await client.post(
        "/api/v1/uploads",
        headers={"Authorization": f"Bearer {advisor_token}"},
        files={"file": ("license.pdf", io.BytesIO(file_content), "application/pdf")},
        data={"category": "credential"},
    )
    assert resp.status_code == 201
    result = resp.json()["data"]
    assert result["category"] == "credential"
    assert result["file_key"].startswith("credential/")
    assert result["file_url"] != ""
    assert result["file_size_bytes"] > 0


async def test_advisor_onboarding_submit_with_document(
    client: AsyncClient, advisor_token: str
) -> None:
    """Full onboarding: upload via global endpoint then reference key in the final submit."""
    # Upload document first via global endpoint
    file_content = b"%PDF-1.4 fake certification"
    upload_resp = await client.post(
        "/api/v1/uploads",
        headers={"Authorization": f"Bearer {advisor_token}"},
        files={"file": ("cert.pdf", io.BytesIO(file_content), "application/pdf")},
        data={"category": "credential"},
    )
    assert upload_resp.status_code == 201
    file_key = upload_resp.json()["data"]["file_key"]

    # Final wizard submit including the document reference
    resp = await client.post(
        "/api/v1/advisors/me/onboarding",
        headers={"Authorization": f"Bearer {advisor_token}"},
        json={
            "title": "Visa Expert",
            "bio": "Helping clients.",
            "visa_specializations": ["family"],
            "documents": [
                {
                    "file_key": file_key,
                    "document_type": "certification",
                    "document_name": "Certification 2024",
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "Visa Expert"


async def test_advisor_onboarding_rejects_foreign_document_key(
    client: AsyncClient, advisor_token: str
) -> None:
    """Submitting a file_key that doesn't belong to this advisor is rejected."""
    resp = await client.post(
        "/api/v1/advisors/me/onboarding",
        headers={"Authorization": f"Bearer {advisor_token}"},
        json={
            "documents": [
                {
                    "file_key": "credential/00000000-0000-0000-0000-000000000000/evil.pdf",
                    "document_type": "certification",
                    "document_name": "Stolen doc",
                }
            ]
        },
    )
    assert resp.status_code == 403


async def test_seeker_cannot_use_advisor_onboarding(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "s2@test.com", "password": "pass1234!", "full_name": "Seeker"},
    )
    login = await client.post(
        "/api/v1/auth/login", data={"username": "s2@test.com", "password": "pass1234!"}
    )
    token = login.json()["access_token"]

    resp = await client.post(
        "/api/v1/advisors/me/onboarding",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Nope"},
    )
    assert resp.status_code == 403


async def test_pending_advisor_can_manage_own_profile(client: AsyncClient) -> None:
    """Advisors must be able to fill their profile during onboarding before admin approval."""
    await client.post(
        "/api/v1/auth/register/advisor",
        json={"email": "pending@test.com", "password": "pass1234!", "full_name": "Pending Advisor"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "pending@test.com", "password": "pass1234!"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # GET profile — should return blank profile, not 403
    resp = await client.get("/api/v1/advisors/me/profile", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["bio"] is None

    # PATCH profile — should succeed, not 403
    resp = await client.patch(
        "/api/v1/advisors/me/profile",
        headers=headers,
        json={"bio": "Onboarding bio", "visa_specializations": ["work"]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["bio"] == "Onboarding bio"
