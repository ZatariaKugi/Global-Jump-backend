"""File storage for credential uploads.

Saves to S3 when ``settings.S3_BUCKET_NAME`` is configured; otherwise falls back to
local disk under ``settings.UPLOAD_DIR/{subdir}/{unique_filename}``, served via the
``/uploads`` static mount. Callers only see the returned URL path and never need to
know which backend is active.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiofiles
import anyio
import boto3
from botocore.exceptions import ClientError
from fastapi import UploadFile

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError

_ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".docx"}


@lru_cache
def _s3_client(access_key: str, secret_key: str, region: str) -> Any:
    # Explicit regional endpoint — the default global endpoint (s3.amazonaws.com) 302s to
    # the regional one for buckets outside us-east-1, which breaks presigned URL signatures.
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
    )


def _s3_enabled(settings: Settings) -> bool:
    return bool(
        settings.S3_BUCKET_NAME and settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY
    )


def _client(settings: Settings) -> Any:
    # Only called when _s3_enabled(settings) has confirmed these are not None.
    assert settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY
    return _s3_client(
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_REGION,
    )


async def save_upload(file: UploadFile, subdir: str, settings: Settings) -> tuple[str, int]:
    """Persist *file* and return ``(url_path, size_bytes)``.

    The returned URL path is relative to the server root, e.g.
    ``/uploads/credentials/<user_id>/<uuid>.pdf``.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise AppError(
            f"File type not allowed. Accepted: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
            code="invalid_file_type",
        )

    content = await file.read()
    size = len(content)
    if size > settings.UPLOAD_MAX_MB * 1024 * 1024:
        raise AppError(
            f"File exceeds maximum size of {settings.UPLOAD_MAX_MB} MB",
            code="file_too_large",
        )
    if size == 0:
        raise AppError("Uploaded file is empty", code="empty_file")

    filename = f"{uuid.uuid4().hex}{suffix}"
    key = f"{subdir}/{filename}"

    if _s3_enabled(settings):
        client = _client(settings)
        try:
            await anyio.to_thread.run_sync(
                lambda: client.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=content)
            )
        except ClientError as exc:
            raise AppError("Failed to store uploaded file", code="storage_error") from exc
        return f"/uploads/{key}", size

    dest_dir = Path(settings.UPLOAD_DIR) / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    async with aiofiles.open(dest_path, "wb") as f:
        await f.write(content)

    return f"/uploads/{key}", size


def resolve_url(url_path: str, settings: Settings) -> str:
    """Turn a stored ``/uploads/{key}`` path into a URL the client can actually fetch.

    S3 keeps "Block all public access" on, so reads need a time-limited presigned URL
    generated per-request rather than a permanent public link. Local storage already
    serves directly from the static mount, so the path is returned unchanged.
    """
    if not url_path.startswith("/uploads/") or not _s3_enabled(settings):
        return url_path

    key = url_path.removeprefix("/uploads/")
    client = _client(settings)
    return str(
        client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key},
            ExpiresIn=3600,
        )
    )


def normalize_file_key(file_key: str) -> str:
    """Strip optional ``/uploads/`` prefix and reject path-traversal attempts."""
    key = file_key.strip().removeprefix("/uploads/").lstrip("/")
    if not key or key != Path(key).as_posix() or ".." in Path(key).parts:
        raise AppError("Invalid file key", code="invalid_file_key")
    return key


def get_upload_by_key(file_key: str, settings: Settings) -> tuple[str, int]:
    """Look up a previously uploaded file by ``file_key``.

    Returns ``(url_path, size_bytes)`` where ``url_path`` is the stored
    ``/uploads/{key}`` form. Raises ``NotFoundError`` when the object is missing.
    """
    key = normalize_file_key(file_key)

    if _s3_enabled(settings):
        client = _client(settings)
        try:
            head = client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        except ClientError as exc:
            code = (exc.response.get("Error") or {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise NotFoundError("File not found") from exc
            raise AppError("Failed to read uploaded file", code="storage_error") from exc
        size = int(head.get("ContentLength") or 0)
        return f"/uploads/{key}", size

    full_path = Path(settings.UPLOAD_DIR) / key
    if not full_path.is_file():
        raise NotFoundError("File not found")
    return f"/uploads/{key}", full_path.stat().st_size


def delete_file(url_path: str, settings: Settings) -> None:
    """Remove a previously saved file (best-effort; ignores missing files)."""
    if not url_path.startswith("/uploads/"):
        return
    key = url_path.removeprefix("/uploads/")

    if _s3_enabled(settings):
        client = _client(settings)
        with contextlib.suppress(ClientError):
            client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        return

    full_path = Path(settings.UPLOAD_DIR) / key
    with contextlib.suppress(FileNotFoundError):
        os.remove(full_path)
