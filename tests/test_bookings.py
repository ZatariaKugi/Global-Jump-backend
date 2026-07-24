"""Booking lifecycle tests (epic #8, PRD §3.6)."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime, time, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.booking import Booking
from tests.test_advisor_search import _make_advisor

BOOKINGS = "/api/v1/bookings"
AVAIL = "/api/v1/advisors/me/availability"


def _next_weekday(weekday: int) -> date:
    today = datetime.now(UTC).date()
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


async def _seeker(client: AsyncClient, email: str = "cust@test.com") -> tuple[str, dict]:
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "custpass123", "full_name": "Seeker"},
    )
    login = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "custpass123"}
    )
    token = str(login.json()["access_token"])
    return token, {"Authorization": f"Bearer {token}"}


async def _bookable_advisor(
    client: AsyncClient, engine, email: str = "adv@test.com"
) -> tuple[str, dict, date]:
    """Advisor with a 30-min service, available all-day UTC on the next Wednesday."""
    advisor_id, token = await _make_advisor(
        client,
        engine,
        email,
        "Bookable Advisor",
        {
            "services": [
                {
                    "service_type": "immigration_specialist",
                    "duration_minutes": 30,
                    "price_usd": 75.0,
                }
            ]
        },
    )
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put(
        AVAIL,
        json={
            "slots": [{"weekday": 2, "start_time": "00:00", "end_time": "23:30", "timezone": "UTC"}]
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return advisor_id, headers, _next_weekday(2)


def _slot_iso(day: date, hour: int, minute: int = 0) -> str:
    return datetime.combine(day, time(hour, minute), UTC).isoformat()


async def test_create_booking_happy_path(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, headers = await _seeker(client)

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, 10),
            "seeker_note": "First consultation",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["status"] == "pending"  # advisor must accept before it's confirmed
    assert data["payment_status"] == "unpaid"
    assert data["service_type"] == "immigration_specialist"
    assert data["duration_minutes"] == 30
    assert data["price_usd"] == 75.0
    assert data["advisor_name"] == "Bookable Advisor"
    assert data["scheduled_end"].startswith(f"{day}T10:30")


async def test_double_booking_rejected(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, headers = await _seeker(client)

    payload = {
        "advisor_id": advisor_id,
        "service_type": "immigration_specialist",
        "scheduled_start": _slot_iso(day, 10),
    }
    assert (await client.post(BOOKINGS, json=payload, headers=headers)).status_code == 201

    _, headers2 = await _seeker(client, "cust2@test.com")
    resp = await client.post(BOOKINGS, json=payload, headers=headers2)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "slot_unavailable"


async def test_booking_outside_availability_rejected(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, headers = await _seeker(client)

    # Next Thursday — advisor only works Wednesdays.
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day + timedelta(days=1), 10),
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "slot_unavailable"


async def test_unknown_service_rejected(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, headers = await _seeker(client)

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "sos_review",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_service"


async def test_advisor_cannot_create_booking(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=advisor_headers,
    )
    assert resp.status_code == 403


async def test_role_scoped_lists_and_detail_isolation(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, 9),
        },
        headers=cust_headers,
    )
    booking_id = resp.json()["data"]["id"]

    # Seeker and advisor both see the booking in their lists.
    for headers in (cust_headers, advisor_headers):
        resp = await client.get(BOOKINGS, headers=headers)
        assert resp.json()["meta"]["pagination"]["total"] == 1

    # A stranger gets 404 on the detail.
    _, stranger_headers = await _seeker(client, "stranger@test.com")
    resp = await client.get(f"{BOOKINGS}/{booking_id}", headers=stranger_headers)
    assert resp.status_code == 404
    resp = await client.get(BOOKINGS, headers=stranger_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 0


async def test_seeker_late_cancellation_blocked(client: AsyncClient, engine) -> None:
    """Booking ~tomorrow with a huge notice window → seeker cancel rejected."""
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    # Raise the advisor's notice requirement above the lead time of the booking.
    resp = await client.patch(
        "/api/v1/advisors/me/profile",
        json={},
        headers=advisor_headers,
    )
    assert resp.status_code == 200

    from sqlalchemy import update

    from app.models.advisor_profile import AdvisorProfile

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(
            update(AdvisorProfile)
            .where(AdvisorProfile.user_id == uuid.UUID(advisor_id))
            .values(cancellation_notice_hours=24 * 30)  # 30 days
        )
        await session.commit()

    _, cust_headers = await _seeker(client)
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=cust_headers,
    )
    booking_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/cancel", json={"reason": "changed mind"}, headers=cust_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "late_cancellation"

    # The advisor can still cancel any time.
    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/cancel", json={"reason": "emergency"}, headers=advisor_headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"


async def test_seeker_cancel_with_enough_notice(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day + timedelta(days=14), 10),
        },
        headers=cust_headers,
    )
    booking_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/cancel", json={"reason": "plans changed"}, headers=cust_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "cancelled"
    assert data["cancellation_reason"] == "plans changed"

    # A cancelled booking cannot be cancelled again.
    resp = await client.post(f"{BOOKINGS}/{booking_id}/cancel", json={}, headers=cust_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


async def test_reschedule_moves_and_validates(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)

    far_day = day + timedelta(days=14)
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(far_day, 10),
        },
        headers=cust_headers,
    )
    booking_id = resp.json()["data"]["id"]

    # Move to another free slot on the same day.
    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/reschedule",
        json={"scheduled_start": _slot_iso(far_day, 14)},
        headers=cust_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["scheduled_start"].startswith(f"{far_day}T14:00")

    # Rescheduling to a non-working day fails.
    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/reschedule",
        json={"scheduled_start": _slot_iso(far_day + timedelta(days=1), 10)},
        headers=cust_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "slot_unavailable"


async def test_complete_and_no_show_transitions(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=cust_headers,
    )
    booking_id = resp.json()["data"]["id"]

    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "confirmed"

    # Before the session starts, completing is rejected.
    resp = await client.post(f"{BOOKINGS}/{booking_id}/complete", headers=advisor_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"

    # Seekers can never complete.
    resp = await client.post(f"{BOOKINGS}/{booking_id}/complete", headers=cust_headers)
    assert resp.status_code == 403

    # Backdate the booking so the session has "started".
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        booking = await session.get(Booking, uuid.UUID(booking_id))
        assert booking is not None
        booking.scheduled_start = datetime.now(UTC) - timedelta(hours=1)
        booking.scheduled_end = datetime.now(UTC) - timedelta(minutes=30)
        await session.commit()

    resp = await client.post(f"{BOOKINGS}/{booking_id}/complete", headers=advisor_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "completed"

    # Completed bookings can't be marked no-show.
    resp = await client.post(f"{BOOKINGS}/{booking_id}/no-show", headers=advisor_headers)
    assert resp.status_code == 400


async def test_booking_in_past_rejected(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": datetime.now(UTC).replace(year=2020).isoformat(),
        },
        headers=cust_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_booking"


# ── Accept / reject workflow ─────────────────────────────────────────────────


async def _pending_booking(
    client: AsyncClient, engine, hour: int = 10
) -> tuple[str, dict, dict, str]:
    """Returns (booking_id, advisor_headers, seeker_headers, day) for a fresh pending booking."""
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, hour),
        },
        headers=cust_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"], advisor_headers, cust_headers, day


async def test_advisor_can_accept_pending_booking(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, _ = await _pending_booking(client, engine)
    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "confirmed"


async def test_seeker_cannot_accept_booking(client: AsyncClient, engine) -> None:
    booking_id, _, cust_headers, _ = await _pending_booking(client, engine)
    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=cust_headers)
    assert resp.status_code == 403


async def test_advisor_can_reject_pending_booking(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, _ = await _pending_booking(client, engine)
    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/reject",
        json={"reason": "Not a good fit"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "rejected"
    assert data["cancellation_reason"] == "Not a good fit"


async def test_cannot_accept_already_confirmed_booking(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, _ = await _pending_booking(client, engine)
    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=advisor_headers)
    assert resp.status_code == 200

    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=advisor_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


# ── Mark important / interpreter ─────────────────────────────────────────────


async def test_mark_important_toggle(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, cust_headers, _ = await _pending_booking(client, engine)

    resp = await client.patch(
        f"{BOOKINGS}/{booking_id}/important", json={"is_important": True}, headers=advisor_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_important"] is True

    # Seekers cannot flag a booking as important.
    resp = await client.patch(
        f"{BOOKINGS}/{booking_id}/important", json={"is_important": False}, headers=cust_headers
    )
    assert resp.status_code == 403


async def test_set_interpreter(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, _ = await _pending_booking(client, engine)

    resp = await client.put(
        f"{BOOKINGS}/{booking_id}/interpreter",
        json={"name": "Jane Doe", "contact": "jane@interpreters.com", "language": "French"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["interpreter_name"] == "Jane Doe"
    assert data["interpreter_contact"] == "jane@interpreters.com"
    assert data["interpreter_language"] == "French"


# ── Per-seeker history (advisor drill-down) ──────────────────────────────────


async def test_seeker_id_filter_scopes_advisor_history(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, seeker1_headers = await _seeker(client, "seeker1@test.com")
    _, seeker2_headers = await _seeker(client, "seeker2@test.com")

    for headers, hour in ((seeker1_headers, 9), (seeker2_headers, 11)):
        resp = await client.post(
            BOOKINGS,
            json={
                "advisor_id": advisor_id,
                "service_type": "immigration_specialist",
                "scheduled_start": _slot_iso(day, hour),
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

    seeker1_id = await _user_id(client, seeker1_headers)
    resp = await client.get(f"{BOOKINGS}?seeker_id={seeker1_id}", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["seeker_id"] == seeker1_id


async def _user_id(client: AsyncClient, headers: dict) -> str:
    resp = await client.get("/api/v1/users/me", headers=headers)
    return str(resp.json()["data"]["id"])


# ── Booking notes ─────────────────────────────────────────────────────────────


async def test_advisor_can_create_and_list_note_with_attachment(
    client: AsyncClient, engine
) -> None:
    booking_id, advisor_headers, cust_headers, _ = await _pending_booking(client, engine)

    upload = await client.post(
        "/api/v1/uploads",
        headers=advisor_headers,
        files={"file": ("checklist.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"category": "booking_note"},
    )
    assert upload.status_code == 201, upload.text
    file_info = upload.json()["data"]

    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/notes",
        json={
            "body": "Please review before our session.",
            "attachments": [
                {
                    "file_key": file_info["file_key"],
                    "file_name": "checklist.pdf",
                    "file_size_bytes": file_info["file_size_bytes"],
                    "content_type": "application/pdf",
                }
            ],
        },
        headers=advisor_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["body"] == "Please review before our session."
    assert len(data["attachments"]) == 1

    # Seeker can read the note.
    resp = await client.get(f"{BOOKINGS}/{booking_id}/notes", headers=cust_headers)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_seeker_cannot_create_note(client: AsyncClient, engine) -> None:
    booking_id, _, cust_headers, _ = await _pending_booking(client, engine)
    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/notes", json={"body": "hi"}, headers=cust_headers
    )
    assert resp.status_code == 403


async def test_empty_note_rejected(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, _ = await _pending_booking(client, engine)
    resp = await client.post(f"{BOOKINGS}/{booking_id}/notes", json={}, headers=advisor_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_note"


# ── Document requests ─────────────────────────────────────────────────────────


async def test_document_request_create_list_and_fulfill(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, cust_headers, _ = await _pending_booking(client, engine)

    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/document-requests",
        json={"description": "Passport copy"},
        headers=advisor_headers,
    )
    assert resp.status_code == 201, resp.text
    request_id = resp.json()["data"]["id"]
    assert resp.json()["data"]["status"] == "requested"

    resp = await client.get(f"{BOOKINGS}/{booking_id}/document-requests", headers=cust_headers)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1

    upload = await client.post(
        "/api/v1/uploads",
        headers=cust_headers,
        files={"file": ("passport.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"category": "booking_document"},
    )
    assert upload.status_code == 201, upload.text
    file_info = upload.json()["data"]

    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/document-requests/{request_id}/fulfill",
        json={
            "file_key": file_info["file_key"],
            "file_name": "passport.pdf",
            "file_size_bytes": file_info["file_size_bytes"],
            "content_type": "application/pdf",
        },
        headers=cust_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "fulfilled"
    assert data["file_name"] == "passport.pdf"


async def test_other_seeker_cannot_fulfill_document_request(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, _ = await _pending_booking(client, engine)
    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/document-requests",
        json={"description": "Passport copy"},
        headers=advisor_headers,
    )
    request_id = resp.json()["data"]["id"]

    _, other_headers = await _seeker(client, "other-seeker@test.com")
    upload = await client.post(
        "/api/v1/uploads",
        headers=other_headers,
        files={"file": ("passport.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"category": "booking_document"},
    )
    file_info = upload.json()["data"]

    resp = await client.post(
        f"{BOOKINGS}/{booking_id}/document-requests/{request_id}/fulfill",
        json={
            "file_key": file_info["file_key"],
            "file_name": "passport.pdf",
            "file_size_bytes": file_info["file_size_bytes"],
            "content_type": "application/pdf",
        },
        headers=other_headers,
    )
    # Not a party to the booking at all -> 404 on the booking lookup itself.
    assert resp.status_code == 404
