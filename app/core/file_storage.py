"""File storage for credential uploads.

Saves to S3 when ``settings.S3_BUCKET_NAME`` is configured; otherwise falls back to
local disk under ``settings.UPLOAD_DIR/{subdir}/{unique_filename}``, served via the
``/uploads`` static mount. Callers only see the returned URL path and never need to
know which backend is active.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
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
# Seeker portfolio uploads are images + PDF only (no Word).
_SEEKER_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
_MAX_FILE_NAME_LEN = 255
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_PNG_MAGIC = b"\x89PNG"  # 89 50 4E 47
_JPEG_MAGIC = b"\xff\xd8\xff"
_ZIP_MAGIC = b"PK\x03\x04"


def seeker_document_extensions() -> set[str]:
    return set(_SEEKER_DOCUMENT_EXTENSIONS)


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


def assert_safe_file_name(name: str, *, field: str = "File name") -> None:
    """Reject names over 255 chars or containing control characters."""
    if len(name) > _MAX_FILE_NAME_LEN or _CONTROL_CHARS.search(name) is not None:
        raise AppError(
            f"{field} must be at most {_MAX_FILE_NAME_LEN} characters and contain "
            "no control characters",
            code="invalid_file_name",
        )


def _docx_has_content_types(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return "[Content_Types].xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


def assert_file_magic(suffix: str, content: bytes) -> None:
    """Require content to match the declared extension (don't trust the name)."""
    if suffix == ".pdf":
        ok = content.startswith(b"%PDF")
    elif suffix == ".png":
        ok = content.startswith(_PNG_MAGIC)
    elif suffix in {".jpg", ".jpeg"}:
        ok = content.startswith(_JPEG_MAGIC)
    elif suffix == ".docx":
        ok = content.startswith(_ZIP_MAGIC) and _docx_has_content_types(content)
    else:
        ok = False
    if not ok:
        raise AppError(
            "File content does not match the declared type. The file may be corrupt or fake.",
            code="invalid_file",
        )


async def save_upload(
    file: UploadFile,
    subdir: str,
    settings: Settings,
    *,
    allowed_extensions: set[str] | None = None,
) -> tuple[str, int]:
    """Persist *file* and return ``(url_path, size_bytes)``.

    The returned URL path is relative to the server root, e.g.
    ``/uploads/credentials/<user_id>/<uuid>.pdf``.
    """
    original_name = file.filename or ""
    if original_name:
        assert_safe_file_name(original_name)
    suffix = Path(original_name).suffix.lower()
    accepted = allowed_extensions if allowed_extensions is not None else _ALLOWED_EXTENSIONS
    if suffix not in accepted:
        raise AppError(
            f"File type not allowed. Accepted: {', '.join(sorted(accepted))}",
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
    assert_file_magic(suffix, content)

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

    Presign / S3 client failures never raise — callers always get a usable path so
    upload and message APIs cannot 500 solely because signing failed.
    """
    if not url_path.startswith("/uploads/") or not _s3_enabled(settings):
        return url_path

    key = url_path.removeprefix("/uploads/")
    try:
        client = _client(settings)
        return str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key},
                ExpiresIn=3600,
            )
        )
    except Exception:  # noqa: BLE001 — never fail API responses on signing
        return url_path


def resolve_media_url(value: str | None, settings: Settings) -> str | None:
    """Resolve a stored media field for API responses.

    - empty / null → ``None``
    - absolute ``http(s)://`` URL → returned as-is
    - bare file key or ``/uploads/{key}`` → signed/public URL via :func:`resolve_url`
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if trimmed.startswith(("http://", "https://")):
        return trimmed
    path = trimmed if trimmed.startswith("/uploads/") else f"/uploads/{trimmed.lstrip('/')}"
    try:
        return resolve_url(path, settings)
    except Exception:  # noqa: BLE001 — defensive; resolve_url already swallows S3 errors
        return path


def storage_path_from_key(file_key: str) -> str:
    """Canonical short path to persist in DB (never a presigned URL)."""
    return f"/uploads/{normalize_file_key(file_key)}"


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


def stored_url_to_key(file_url: str) -> str | None:
    """Best-effort extract of a storage key from a persisted ``file_url``."""
    trimmed = file_url.strip().split("?", 1)[0]
    marker = "seeker_document/"
    if marker in trimmed:
        return marker + trimmed.split(marker, 1)[1].lstrip("/")
    if trimmed.startswith("/uploads/"):
        return trimmed.removeprefix("/uploads/")
    return None


def sweep_unreferenced_uploads(
    settings: Settings,
    *,
    prefix: str,
    referenced_keys: set[str],
    older_than: timedelta,
) -> int:
    """Delete objects under ``prefix`` older than ``older_than`` and not referenced.

    Used to clean uploads that never became a document row (client died between
    ``POST /uploads`` and ``POST /users/me/documents``).
    """
    cutoff = datetime.now(UTC) - older_than
    deleted = 0
    if _s3_enabled(settings):
        client = _client(settings)
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.S3_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = str(obj["Key"])
                last_modified = obj.get("LastModified")
                if last_modified is None:
                    continue
                if last_modified.tzinfo is None:
                    last_modified = last_modified.replace(tzinfo=UTC)
                if last_modified >= cutoff:
                    continue
                if key in referenced_keys:
                    continue
                with contextlib.suppress(ClientError):
                    client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
                    deleted += 1
        return deleted

    root = Path(settings.UPLOAD_DIR) / prefix.rstrip("/")
    if not root.is_dir():
        return 0
    upload_root = Path(settings.UPLOAD_DIR)
    cutoff_ts = cutoff.timestamp()
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_mtime >= cutoff_ts:
            continue
        key = path.relative_to(upload_root).as_posix()
        if key in referenced_keys:
            continue
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            deleted += 1
    return deleted
