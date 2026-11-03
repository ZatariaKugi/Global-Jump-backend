"""Phase 2 (#185) · flows 7, 8, 9: documents, chat, reviews — seeker and advisor together."""

from __future__ import annotations

import pytest

from tests.flows import booking_helpers as b
from tests.flows import helpers as h
from tests.flows.conftest import World

pytestmark = pytest.mark.asyncio

PDF = b"%PDF-1.4\n%flows\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


async def _upload(
    world: World, headers: dict[str, str], category: str, name: str = "passport.pdf"
) -> dict:
    r = await world.client.post(
        "/api/v1/uploads",
        files={"file": (name, PDF, "application/pdf")},
        data={"category": category},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _completed_consultation(world: World) -> str:
    service_id = await b.create_global_service(world, "Consultation")
    await b.offer_service(world, world.advisor, service_id, price_usd=100)
    await b.open_all_week(world, world.advisor)
    await h.connect_zoom(world, world.advisor)
    start = await b.first_free_slot(world, world.seeker, world.advisor_id)
    booking = await b.book(world, world.seeker, world.advisor_id, service_id, start)
    await b.pay(world, world.seeker, booking["id"])
    from tests.flows.test_flow_consultation_lifecycle import _move_into_the_past

    await _move_into_the_past(world, booking["id"])
    h.body(
        await world.client.post(f"/api/v1/bookings/{booking['id']}/complete", headers=world.advisor)
    )
    return booking["id"]


async def test_seeker_uploads_a_document_and_the_advisor_reviews_it(world: World) -> None:
    booking_id = await _completed_consultation(world)  # the advisor is this seeker's advisor

    up = await _upload(world, world.seeker, "seeker_document")
    doc = h.body(
        await world.client.post(
            "/api/v1/users/me/documents",
            json={
                "file_key": up["file_key"],
                "file_name": "passport.pdf",
                "file_size_bytes": up["file_size_bytes"],
                "content_type": "application/pdf",
                "category": "passport",
                "document_name": "Passport copy",
            },
            headers=world.seeker,
        )
    )
    assert doc["status"] == "under_review"

    # the advisor sees the client's portfolio, approves with a note
    portfolio = h.body(
        await world.client.get(
            f"/api/v1/advisors/me/clients/{world.seeker_id}/documents", headers=world.advisor
        )
    )
    assert [d["id"] for d in portfolio] == [doc["id"]]
    reviewed = h.body(
        await world.client.patch(
            f"/api/v1/advisors/me/clients/{world.seeker_id}/documents/{doc['id']}",
            json={"status": "approved", "note": "Clear scan, thank you."},
            headers=world.advisor,
        )
    )
    assert reviewed["status"] == "approved"
    mine = h.body(await world.client.get("/api/v1/users/me/documents", headers=world.seeker))
    assert mine[0]["status"] == "approved"
    assert "document_status_updated" in await b.notifications(world, world.seeker)

    # the advisor asks for one more; the seeker answers it with an upload
    req = h.body(
        await world.client.post(
            f"/api/v1/bookings/{booking_id}/document-requests",
            json={"description": "Bank statements for the last six months"},
            headers=world.advisor,
        )
    )
    assert "document_requested" in await b.notifications(world, world.seeker)
    up2 = await _upload(world, world.seeker, "booking_document", "bank.pdf")
    fulfilled = h.body(
        await world.client.post(
            f"/api/v1/bookings/{booking_id}/document-requests/{req['id']}/fulfill",
            json={
                "file_key": up2["file_key"],
                "file_name": "bank.pdf",
                "file_size_bytes": up2["file_size_bytes"],
            },
            headers=world.seeker,
        )
    )
    assert fulfilled["status"] in ("fulfilled", "submitted")
    assert "document_fulfilled" in await b.notifications(world, world.advisor)


async def _session_in_progress(world: World) -> str:
    """A paid consultation whose scheduled window contains now — the only time chat is open."""
    import uuid
    from datetime import UTC, datetime, timedelta

    from app.models.booking import Booking

    service_id = await b.create_global_service(world, "Consultation")
    await b.offer_service(world, world.advisor, service_id, price_usd=100)
    await b.open_all_week(world, world.advisor)
    await h.connect_zoom(world, world.advisor)
    start = await b.first_free_slot(world, world.seeker, world.advisor_id)
    booking = await b.book(world, world.seeker, world.advisor_id, service_id, start)
    await b.pay(world, world.seeker, booking["id"])
    async with world.Session() as s:
        row = await s.get(Booking, uuid.UUID(booking["id"]))
        assert row is not None
        row.scheduled_start = datetime.now(UTC) - timedelta(minutes=5)
        row.scheduled_end = datetime.now(UTC) + timedelta(minutes=25)
        await s.commit()
    return booking["id"]


async def test_chat_is_closed_outside_a_session_and_open_during_one(world: World) -> None:
    conv = h.body(
        await world.client.post(
            "/api/v1/conversations",
            json={"other_user_id": str(world.advisor_id)},
            headers=world.seeker,
        )
    )
    # no session in progress: the thread exists but nothing can be sent
    closed = await world.client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"body": "Hello?"},
        headers=world.seeker,
    )
    assert closed.status_code == 400 and closed.json()["error"]["code"] == "chat_inactive"

    await _session_in_progress(world)
    up = await _upload(world, world.seeker, "message_attachment", "question.pdf")
    sent = h.body(
        await world.client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={
                "body": "Hello, can you help with a study visa?",
                "attachments": [
                    {
                        "file_key": up["file_key"],
                        "file_name": "question.pdf",
                        "file_size_bytes": up["file_size_bytes"],
                    }
                ],
            },
            headers=world.seeker,
        )
    )
    assert sent["body"].startswith("Hello") and len(sent["attachments"]) == 1
    assert "message_received" in await b.notifications(world, world.advisor)

    thread = h.body(
        await world.client.get(
            f"/api/v1/conversations/{conv['id']}/messages", headers=world.advisor
        )
    )
    assert [m["id"] for m in thread] == [sent["id"]]
    read = h.body(
        await world.client.patch(f"/api/v1/messages/{sent['id']}/read", headers=world.advisor)
    )
    assert read["read_at"]

    reply = h.body(
        await world.client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"body": "Buy my coins now!!!"},
            headers=world.advisor,
        )
    )
    h.body(
        await world.client.post(
            f"/api/v1/messages/{reply['id']}/report", json={"reason": "Spam"}, headers=world.seeker
        )
    )
    queue = h.body(await world.client.get("/api/v1/admin/messages/flagged", headers=world.admin))
    assert [m["id"] for m in queue] == [reply["id"]]
    removed = h.body(
        await world.client.patch(
            f"/api/v1/admin/messages/{reply['id']}/moderation",
            json={"action": "remove"},
            headers=world.admin,
        )
    )
    assert removed["moderation_status"] == "removed"
    thread = h.body(
        await world.client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=world.seeker)
    )
    assert reply["id"] not in [m["id"] for m in thread], (
        "a removed message disappears from the thread"
    )


async def test_only_a_completed_consultation_can_be_reviewed_and_the_advisor_replies(
    world: World,
) -> None:
    booking_id = await _completed_consultation(world)
    review = h.body(
        await world.client.post(
            f"/api/v1/bookings/{booking_id}/review",
            json={
                "rating_expertise": 5,
                "rating_communication": 5,
                "rating_professionalism": 4,
                "rating_value": 4,
                "text": "Very clear.",
            },
            headers=world.seeker,
        )
    )
    assert float(review["rating_overall"]) == 4.5

    dash = h.body(await world.client.get("/api/v1/advisors/me/dashboard", headers=world.advisor))
    assert dash["stats"]["pending_reviews_count"] == 1, "one review awaiting the advisor's reply"

    answered = h.body(
        await world.client.post(
            f"/api/v1/reviews/{review['id']}/response",
            json={"response": "Thank you!"},
            headers=world.advisor,
        )
    )
    assert answered["advisor_response"] == "Thank you!"
    dash = h.body(await world.client.get("/api/v1/advisors/me/dashboard", headers=world.advisor))
    assert dash["stats"]["pending_reviews_count"] == 0

    public = h.body(
        await world.client.get(f"/api/v1/advisors/{world.advisor_id}", headers=world.seeker)
    )
    assert public["average_rating"] == 4.5 and public["review_count"] == 1

    again = await world.client.post(
        f"/api/v1/bookings/{booking_id}/review",
        json={
            "rating_expertise": 1,
            "rating_communication": 1,
            "rating_professionalism": 1,
            "rating_value": 1,
        },
        headers=world.seeker,
    )
    assert again.status_code in (400, 409), "one review per consultation"
