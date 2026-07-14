"""Seeker document portfolio + advisor/admin review tests (PRD §3.8)."""

from __future__ import annotations

import io

from httpx import AsyncClient

from tests.test_bookings import _bookable_advisor, _seeker, _slot_iso

BOOKINGS = "/api/v1/bookings"
DOCUMENTS = "/api/v1/users/me/documents"


async def _upload_seeker_document(client: AsyncClient, headers: dict) -> dict:
    upload = await client.post(
        "/api/v1/uploads",
        headers=headers,
        files={"file": ("passport.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"category": "seeker_document"},
    )
    assert upload.status_code == 201, upload.text
    file_info = upload.json()["data"]

    resp = await client.post(
        DOCUMENTS,
        json={
            "file_key": file_info["file_key"],
            "file_name": "passport.pdf",
            "file_size_bytes": file_info["file_size_bytes"],
            "content_type": "application/pdf",
            "category": "passport",
            "document_name": "Passport Copy",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _booked_pair(client: AsyncClient, engine) -> tuple[str, dict, dict]:
    """Returns (advisor_id, advisor_headers, seeker_headers) with an existing booking."""
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, seeker_headers = await _seeker(client)
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=seeker_headers,
    )
    assert resp.status_code == 201, resp.text
    return advisor_id, advisor_headers, seeker_headers


async def _user_id(client: AsyncClient, headers: dict) -> str:
    resp = await client.get("/api/v1/users/me", headers=headers)
    return str(resp.json()["data"]["id"])


# ── Seeker upload / list / comment ───────────────────────────────────────────


async def test_seeker_can_upload_list_and_comment(client: AsyncClient) -> None:
    _, seeker_headers = await _seeker(client)
    document = await _upload_seeker_document(client, seeker_headers)
    assert document["category"] == "passport"
    assert document["status"] == "under_review"

    resp = await client.get(DOCUMENTS, headers=seeker_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.post(
        f"{DOCUMENTS}/{document['id']}/comments",
        json={"body": "Uploaded my passport, please review."},
        headers=seeker_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(f"{DOCUMENTS}/{document['id']}/comments", headers=seeker_headers)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_seeker_cannot_comment_on_another_seekers_document(client: AsyncClient) -> None:
    _, seeker_headers = await _seeker(client, "owner@test.com")
    document = await _upload_seeker_document(client, seeker_headers)

    _, other_headers = await _seeker(client, "other-seeker@test.com")
    resp = await client.post(
        f"{DOCUMENTS}/{document['id']}/comments", json={"body": "hi"}, headers=other_headers
    )
    assert resp.status_code == 404


# ── Advisor review ────────────────────────────────────────────────────────────


async def test_advisor_with_relationship_can_review_and_comment(
    client: AsyncClient, engine
) -> None:
    advisor_id, advisor_headers, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)
    document = await _upload_seeker_document(client, seeker_headers)

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents", headers=advisor_headers
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.patch(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}",
        json={"status": "approved"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "approved"
    assert data["reviewed_by"] == advisor_id

    resp = await client.post(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}/comments",
        json={"body": "Looks good, thanks!"},
        headers=advisor_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents/{document['id']}/comments",
        headers=advisor_headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_advisor_without_relationship_gets_404(client: AsyncClient, engine) -> None:
    _, _, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)
    await _upload_seeker_document(client, seeker_headers)

    # A second advisor with no booking relationship to this seeker.
    _, unrelated_advisor_headers, _ = await _bookable_advisor(client, engine, "unrelated@test.com")

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents", headers=unrelated_advisor_headers
    )
    assert resp.status_code == 404


async def test_seeker_forbidden_from_advisor_document_endpoints(
    client: AsyncClient, engine
) -> None:
    _, _, seeker_headers = await _booked_pair(client, engine)
    seeker_id = await _user_id(client, seeker_headers)

    resp = await client.get(
        f"/api/v1/advisors/me/clients/{seeker_id}/documents", headers=seeker_headers
    )
    assert resp.status_code == 403


# ── Admin access ──────────────────────────────────────────────────────────────


async def test_admin_can_review_any_seeker_document(
    client: AsyncClient, engine, admin_token: str
) -> None:
    _, seeker_headers = await _seeker(client)
    seeker_id = await _user_id(client, seeker_headers)
    document = await _upload_seeker_document(client, seeker_headers)

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(f"/api/v1/admin/seekers/{seeker_id}/documents", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.patch(
        f"/api/v1/admin/seekers/{seeker_id}/documents/{document['id']}",
        json={"status": "rejected"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "rejected"
