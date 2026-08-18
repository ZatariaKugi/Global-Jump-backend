"""Tests for the global file upload endpoint."""

from __future__ import annotations

import io
import os
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import UploadFile
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.file_storage import (
    assert_safe_file_name,
    save_upload,
    sweep_unreferenced_uploads,
)


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


async def test_save_upload_rejects_extension_spoofed_pdf() -> None:
    settings = get_settings()
    upload = UploadFile(filename="fake.pdf", file=io.BytesIO(b"not-a-pdf"))
    with pytest.raises(AppError) as exc:
        await save_upload(upload, "seeker_document/test", settings)
    assert exc.value.code == "invalid_file"


async def test_save_upload_rejects_control_character_filename() -> None:
    settings = get_settings()
    upload = UploadFile(filename="bad\x00name.pdf", file=io.BytesIO(b"%PDF-1.4 x"))
    with pytest.raises(AppError) as exc:
        await save_upload(upload, "seeker_document/test", settings)
    assert exc.value.code == "invalid_file_name"


def test_assert_safe_file_name_rejects_overlong() -> None:
    with pytest.raises(AppError) as exc:
        assert_safe_file_name("a" * 256)
    assert exc.value.code == "invalid_file_name"


def test_orphan_sweep_deletes_old_unreferenced_files(tmp_path: Path) -> None:
    settings = get_settings().model_copy(update={"UPLOAD_DIR": str(tmp_path)})
    folder = tmp_path / "seeker_document" / "uid"
    folder.mkdir(parents=True)
    old = folder / "old.pdf"
    kept = folder / "kept.pdf"
    fresh = folder / "fresh.pdf"
    old.write_bytes(b"%PDF-1.4 old")
    kept.write_bytes(b"%PDF-1.4 kept")
    fresh.write_bytes(b"%PDF-1.4 fresh")
    os.utime(old, (1, 1))
    os.utime(kept, (1, 1))

    deleted = sweep_unreferenced_uploads(
        settings,
        prefix="seeker_document/",
        referenced_keys={"seeker_document/uid/kept.pdf"},
        older_than=timedelta(hours=24),
    )
    assert deleted == 1
    assert not old.exists()
    assert kept.exists()
    assert fresh.exists()
