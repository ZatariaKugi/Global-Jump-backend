"""Global file upload endpoint — usable by any authenticated user in any feature."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Response, UploadFile

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.core.exceptions import AppError, PermissionDeniedError
from app.core.file_storage import (
    delete_file,
    get_upload_by_key,
    normalize_file_key,
    resolve_url,
    save_upload,
)
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.upload import UploadCategory, UploadResult

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _category_from_key(file_key: str) -> UploadCategory:
    segment = file_key.split("/", 1)[0]
    try:
        return UploadCategory(segment)
    except ValueError as exc:
        raise AppError("Unknown upload category in file key", code="invalid_file_key") from exc


def _assert_owns_file_key(file_key: str, user_id: object) -> None:
    """Keys are stored as ``{category}/{user_id}/{uuid}{ext}``."""
    parts = file_key.split("/")
    if len(parts) < 3 or parts[1] != str(user_id):
        raise PermissionDeniedError("You can only delete your own uploads")


@router.post("", status_code=201, response_model=ResponseEnvelope[UploadResult])
async def upload_file(
    current_user: CurrentUser,
    settings: SettingsDep,
    request_id: RequestIdDep,
    file: UploadFile,
    category: Annotated[UploadCategory, Form()],
) -> ResponseEnvelope[UploadResult]:
    """Upload any file and receive back a ``file_key``.

    The key can then be passed to any domain endpoint that needs to record
    a file reference.  Files are stored under ``{category}/{user_id}/{uuid}{ext}``
    so uploads from different features are isolated.

    **Categories:**

    - ``credential`` — advisor verification documents (immigration license,
      bar membership, government ID, etc.).  Pass the returned key in
      ``POST /advisors/me/onboarding`` → ``documents[].file_key`` or
      ``POST /advisors/me/credentials`` → ``file_key``. Preferred alias:
      ``advisor_document``.
    - ``advisor_document`` — same as ``credential`` (advisor verification /
      portfolio documents). Prefer this name for new clients; ``credential``
      remains accepted for backwards compatibility.
    - ``profile_photo`` — advisor or seeker profile picture.  Pass the
      returned key in ``PATCH /advisors/me/profile`` or
      ``PATCH /users/me/profile`` → ``profile_photo_url``.
    - ``message_attachment`` — files attached to a conversation message.
      Pass the returned key in ``POST /conversations/{id}/messages``
      → ``attachments[].file_key``.
    - ``booking_note`` — files attached to an advisor's note on a booking.
      Pass the returned key in ``POST /bookings/{id}/notes`` → ``attachments[].file_key``.
    - ``booking_document`` — a document fulfilling an advisor's document request.
      Pass the returned key in
      ``POST /bookings/{id}/document-requests/{request_id}/fulfill`` → ``file_key``.
    - ``seeker_document`` — a document added to the seeker's document portfolio
      (passport, educational, finance, supporting). Pass the returned key in
      ``POST /users/me/documents`` → ``file_key``.
    - ``general`` — anything else that does not fit the categories above.

    **Accepted formats:** pdf, jpg, jpeg, png, docx.
    """
    subdir = f"{category.value}/{current_user.id}"
    url_path, size = await save_upload(file, subdir, settings)
    file_key = url_path.removeprefix("/uploads/")
    return ResponseEnvelope[UploadResult](
        data=UploadResult(
            file_key=file_key,
            file_url=resolve_url(url_path, settings),
            category=category,
            file_size_bytes=size,
        ),
        meta=Meta(request_id=request_id),
    )


@router.get("/{file_key:path}", response_model=ResponseEnvelope[UploadResult])
async def get_upload_by_file_key(
    file_key: str,
    _current_user: CurrentUser,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UploadResult]:
    """Resolve a previously uploaded ``file_key`` to a fresh ``file_url`` + metadata.

    Useful when a client stored only the key and needs a (possibly refreshed
    S3-presigned) URL for preview or download.
    """
    key = normalize_file_key(file_key)
    url_path, size = get_upload_by_key(key, settings)
    return ResponseEnvelope[UploadResult](
        data=UploadResult(
            file_key=key,
            file_url=resolve_url(url_path, settings),
            category=_category_from_key(key),
            file_size_bytes=size,
        ),
        meta=Meta(request_id=request_id),
    )


@router.delete("/{file_key:path}", status_code=204)
async def delete_upload_by_file_key(
    file_key: str,
    current_user: CurrentUser,
    settings: SettingsDep,
) -> Response:
    """Delete a previously uploaded file owned by the current user.

    Keys follow ``{category}/{user_id}/{uuid}{ext}``; callers may only delete
    their own uploads. Missing keys return 404.
    """
    key = normalize_file_key(file_key)
    _assert_owns_file_key(key, current_user.id)
    url_path, _size = get_upload_by_key(key, settings)
    delete_file(url_path, settings)
    return Response(status_code=204)
