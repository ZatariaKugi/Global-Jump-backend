"""Schemas for the global file upload endpoint."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class UploadCategory(StrEnum):
    """Allowed upload categories — each maps to a storage subdirectory.

    The server routes files to ``{category}/{user_id}/`` so uploads from
    different features never collide and access control is easy to audit.
    """

    profile_photo = "profile_photo"
    credential = "credential"
    message_attachment = "message_attachment"
    booking_note = "booking_note"
    booking_document = "booking_document"
    seeker_document = "seeker_document"
    ticket_attachment = "ticket_attachment"
    general = "general"


class UploadResult(BaseModel):
    """Returned by the global upload endpoint.

    ``file_key`` is the storage-relative path (e.g.
    ``credential/abc-123/uuid.pdf``).  Clients store this key and pass it back
    to whichever domain endpoint needs to record the file (onboarding submit,
    profile update, message send, etc.).  ``file_url`` is a ready-to-use URL
    for immediate preview — presigned S3 or a local ``/uploads`` path.
    """

    file_key: str
    file_url: str
    category: UploadCategory
    file_size_bytes: int
