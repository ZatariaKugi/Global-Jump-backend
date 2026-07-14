"""Tests for advisor credential document upload and admin review."""

from __future__ import annotations

import io

from httpx import AsyncClient


async def _upload_and_create_credential(
    client: AsyncClient,
    token: str,
    document_type: str = "immigration_license",
    document_name: str = "My License",
) -> dict:
    """Upload via global endpoint then create the credential record."""
    upload = await client.post(
        "/api/v1/uploads",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("license.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"category": "credential"},
    )
    assert upload.status_code == 201, upload.text
    file_key = upload.json()["data"]["file_key"]

    return await client.post(
        "/api/v1/advisors/me/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={"file_key": file_key, "document_type": document_type, "document_name": document_name},
    )


async def test_upload_credential(client: AsyncClient, advisor_token: str) -> None:
    resp = await _upload_and_create_credential(client, advisor_token)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "pending"
    assert data["document_type"] == "immigration_license"
    assert data["document_name"] == "My License"


async def test_list_my_credentials(client: AsyncClient, advisor_token: str) -> None:
    await _upload_and_create_credential(
        client, advisor_token, document_type="certification", document_name="ICCRC Cert"
    )

    resp = await client.get(
        "/api/v1/advisors/me/credentials",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) >= 1


async def test_delete_pending_credential(client: AsyncClient, advisor_token: str) -> None:
    upload_resp = await _upload_and_create_credential(
        client, advisor_token, document_type="government_id", document_name="Passport Copy"
    )
    cred_id = upload_resp.json()["data"]["id"]

    resp = await client.delete(
        f"/api/v1/advisors/me/credentials/{cred_id}",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    assert resp.status_code == 204

    list_resp = await client.get(
        "/api/v1/advisors/me/credentials",
        headers={"Authorization": f"Bearer {advisor_token}"},
    )
    ids = [c["id"] for c in list_resp.json()["data"]]
    assert cred_id not in ids


async def test_admin_can_list_advisor_credentials(
    client: AsyncClient, advisor_token: str, admin_token: str
) -> None:
    me_resp = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {advisor_token}"}
    )
    advisor_id = me_resp.json()["data"]["id"]

    await _upload_and_create_credential(
        client, advisor_token, document_type="bar_membership", document_name="Bar Card"
    )

    resp = await client.get(
        f"/api/v1/admin/advisors/{advisor_id}/credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) >= 1


async def test_admin_can_verify_credential(
    client: AsyncClient, advisor_token: str, admin_token: str
) -> None:
    me_resp = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {advisor_token}"}
    )
    advisor_id = me_resp.json()["data"]["id"]

    upload_resp = await _upload_and_create_credential(
        client, advisor_token, document_type="immigration_license", document_name="License 2026"
    )
    cred_id = upload_resp.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/admin/advisors/{advisor_id}/credentials/{cred_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "verified", "admin_note": "Documents look good"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "verified"
    assert data["admin_note"] == "Documents look good"
    assert data["verified_at"] is not None


async def test_invalid_file_key_rejected(client: AsyncClient, advisor_token: str) -> None:
    """A file_key that doesn't belong to this advisor is rejected."""
    resp = await client.post(
        "/api/v1/advisors/me/credentials",
        headers={"Authorization": f"Bearer {advisor_token}"},
        json={
            "file_key": "credential/00000000-0000-0000-0000-000000000000/evil.pdf",
            "document_type": "other",
            "document_name": "Stolen",
        },
    )
    assert resp.status_code == 403


async def test_seeker_cannot_upload_credentials(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "cust3@test.com", "password": "pass1234!", "full_name": "Cust3"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "cust3@test.com", "password": "pass1234!"},
    )
    token = login.json()["access_token"]

    resp = await client.post(
        "/api/v1/advisors/me/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "file_key": "credential/some-id/f.pdf",
            "document_type": "other",
            "document_name": "Test",
        },
    )
    assert resp.status_code == 403
