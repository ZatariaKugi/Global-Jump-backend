"""Tests for the global file upload endpoint."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient


@pytest.fixture
async def seeker_token(client: AsyncClient) -> str:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "uploader@test.com", "password": "pass1234!", "full_name": "Uploader"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "uploader@test.com", "password": "pass1234!"},
    )
    assert resp.status_code == 200
    return str(resp.json()["access_token"])


async def _upload(
    client: AsyncClient, token: str, category: str, filename: str = "doc.pdf"
) -> dict:
    content = b"%PDF-1.4 test content"
    return await client.post(
        "/api/v1/uploads",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, io.BytesIO(content), "application/pdf")},
        data={"category": category},
    )


async def test_upload_credential(client: AsyncClient, seeker_token: str) -> None:
    resp = await _upload(client, seeker_token, "credential")
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["category"] == "credential"
    assert data["file_key"].startswith("credential/")
    assert data["file_url"] != ""


async def test_upload_profile_photo(client: AsyncClient, seeker_token: str) -> None:
    content = b"\x89PNG fake image"
    resp = await client.post(
        "/api/v1/uploads",
        headers={"Authorization": f"Bearer {seeker_token}"},
        files={"file": ("photo.png", io.BytesIO(content), "image/png")},
        data={"category": "profile_photo"},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["file_key"].startswith("profile_photo/")


async def test_upload_message_attachment(client: AsyncClient, seeker_token: str) -> None:
    resp = await _upload(client, seeker_token, "message_attachment")
    assert resp.status_code == 201
    assert resp.json()["data"]["file_key"].startswith("message_attachment/")


async def test_upload_general(client: AsyncClient, seeker_token: str) -> None:
    resp = await _upload(client, seeker_token, "general")
    assert resp.status_code == 201
    assert resp.json()["data"]["file_key"].startswith("general/")


async def test_upload_invalid_category(client: AsyncClient, seeker_token: str) -> None:
    content = b"data"
    resp = await client.post(
        "/api/v1/uploads",
        headers={"Authorization": f"Bearer {seeker_token}"},
        files={"file": ("f.pdf", io.BytesIO(content), "application/pdf")},
        data={"category": "not_a_real_category"},
    )
    assert resp.status_code == 422


async def test_upload_disallowed_extension(client: AsyncClient, seeker_token: str) -> None:
    content = b"<script>alert(1)</script>"
    resp = await client.post(
        "/api/v1/uploads",
        headers={"Authorization": f"Bearer {seeker_token}"},
        files={"file": ("evil.html", io.BytesIO(content), "text/html")},
        data={"category": "general"},
    )
    assert resp.status_code == 400


async def test_upload_requires_auth(client: AsyncClient) -> None:
    content = b"%PDF-1.4 test"
    resp = await client.post(
        "/api/v1/uploads",
        files={"file": ("doc.pdf", io.BytesIO(content), "application/pdf")},
        data={"category": "general"},
    )
    assert resp.status_code == 401


async def test_advisor_can_also_upload(client: AsyncClient, advisor_token: str) -> None:
    resp = await _upload(client, advisor_token, "credential")
    assert resp.status_code == 201
    assert resp.json()["data"]["category"] == "credential"
