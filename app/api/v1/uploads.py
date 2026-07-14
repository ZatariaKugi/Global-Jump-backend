"""Global file upload endpoint — usable by any authenticated user in any feature."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, UploadFile

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.core.file_storage import resolve_url, save_upload
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.upload import UploadCategory, UploadResult

router = APIRouter(prefix="/uploads", tags=["uploads"])


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
      ``POST /advisors/me/onboarding`` → ``documents[].file_key``.
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

    **Accepted formats:** pdf, jpg, jpeg, png.
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
