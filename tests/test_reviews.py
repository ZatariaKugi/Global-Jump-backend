"""Ratings & reviews tests (epic #11, PRD §3.9)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.booking import Booking, BookingStatus, PaymentStatus
from tests.test_bookings import _bookable_advisor, _seeker, _slot_iso

REVIEW_BODY = {
    "rating_expertise": 5,
    "rating_communication": 4,
    "rating_professionalism": 5,
    "rating_value": 4,
    "text": "Very helpful session.",
}


async def _completed_booking(
    client: AsyncClient,
    engine,
    cust_headers: dict,
    advisor_id: str,
    day,
    hour: int = 10,
    paid: bool = False,
) -> str:
    """Create a booking, then force it to completed (optionally paid) via the DB."""
    resp = await client.post(
        "/api/v1/bookings",
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, hour),
        },
        headers=cust_headers,
    )
    assert resp.status_code == 201, resp.text
    booking_id = resp.json()["data"]["id"]

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        booking = await session.get(Booking, uuid.UUID(booking_id))
        booking.status = BookingStatus.completed
        booking.scheduled_start = datetime.now(UTC) - timedelta(hours=2)
        booking.scheduled_end = datetime.now(UTC) - timedelta(hours=1)
        if paid:
            booking.payment_status = PaymentStatus.paid
        await session.commit()
    return str(booking_id)


async def test_submit_review_happy_path(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, advisor_id, day)

    resp = await client.post(
        f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["rating_overall"] == 4.5  # (5+4+5+4)/4
    assert data["is_verified"] is False  # unpaid booking → no verified badge
    assert data["seeker_name"] == "Seeker"

    # Only one review per booking.
    resp = await client.post(
        f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "already_reviewed"


async def test_verified_badge_for_paid_booking(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, advisor_id, day, paid=True)

    resp = await client.post(
        f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
    )
    assert resp.json()["data"]["is_verified"] is True


async def test_review_requires_completed_booking(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)

    resp = await client.post(
        "/api/v1/bookings",
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 11),
        },
        headers=cust_headers,
    )
    booking_id = resp.json()["data"]["id"]  # still confirmed, not completed

    resp = await client.post(
        f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "not_completed"


async def test_advisor_cannot_review_own_booking(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, advisor_id, day)

    resp = await client.post(
        f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=advisor_headers
    )
    assert resp.status_code == 403


async def test_public_listing_and_rating_summary(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, advisor_id, day)
    await client.post(
        f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
    )

    resp = await client.get(f"/api/v1/advisors/{advisor_id}/reviews", headers=cust_headers)
    assert resp.status_code == 200
    assert resp.json()["meta"]["pagination"]["total"] == 1

    resp = await client.get(f"/api/v1/advisors/{advisor_id}/rating", headers=cust_headers)
    data = resp.json()["data"]
    assert data["average_rating"] == 4.5
    assert data["review_count"] == 1


async def test_advisor_response_once(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, advisor_id, day)
    review_id = (
        await client.post(
            f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
        )
    ).json()["data"]["id"]

    # Seeker cannot respond.
    resp = await client.post(
        f"/api/v1/reviews/{review_id}/response",
        json={"response": "Thanks!"},
        headers=cust_headers,
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"/api/v1/reviews/{review_id}/response",
        json={"response": "Thank you for the kind words."},
        headers=advisor_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["advisor_response"] == "Thank you for the kind words."
    assert resp.json()["data"]["responded_at"] is not None

    # Only once.
    resp = await client.post(
        f"/api/v1/reviews/{review_id}/response",
        json={"response": "Again!"},
        headers=advisor_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "already_responded"


async def test_report_and_moderation_flow(client: AsyncClient, engine, admin_token: str) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, advisor_id, day)
    review_id = (
        await client.post(
            f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
        )
    ).json()["data"]["id"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Advisor reports the review.
    resp = await client.post(
        f"/api/v1/reviews/{review_id}/report",
        json={"reason": "Inappropriate content"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200

    # It appears in the admin queue.
    resp = await client.get("/api/v1/admin/reviews/flagged", headers=admin_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 1
    assert resp.json()["data"][0]["flag_reason"] == "Inappropriate content"

    # While flagged it is still publicly visible.
    resp = await client.get(f"/api/v1/advisors/{advisor_id}/reviews", headers=cust_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 1

    # Admin removes it → hidden from public listing and aggregate.
    resp = await client.patch(
        f"/api/v1/admin/reviews/{review_id}/moderation",
        json={"action": "remove"},
        headers=admin_headers,
    )
    assert resp.json()["data"]["moderation_status"] == "removed"

    resp = await client.get(f"/api/v1/advisors/{advisor_id}/reviews", headers=cust_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 0
    resp = await client.get(f"/api/v1/advisors/{advisor_id}/rating", headers=cust_headers)
    assert resp.json()["data"]["review_count"] == 0

    # Queue is now empty.
    resp = await client.get("/api/v1/admin/reviews/flagged", headers=admin_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 0


async def test_moderation_requires_admin(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    resp = await client.get("/api/v1/admin/reviews/flagged", headers=cust_headers)
    assert resp.status_code == 403


async def test_rating_in_search_and_listing_card(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, advisor_id, day)
    await client.post(
        f"/api/v1/bookings/{booking_id}/review", json=REVIEW_BODY, headers=cust_headers
    )

    # Listing card now carries the aggregate.
    resp = await client.get("/api/v1/advisors", headers=cust_headers)
    card = resp.json()["data"][0]
    assert card["average_rating"] == 4.5
    assert card["review_count"] == 1

    # min_rating filter: 4.0 keeps the advisor, 4.9 excludes them.
    resp = await client.get("/api/v1/advisors?min_rating=4", headers=cust_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 1
    resp = await client.get("/api/v1/advisors?min_rating=4.9", headers=cust_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 0

    # sort=rating and sort=review_count run without error.
    for sort in ("rating", "review_count"):
        resp = await client.get(f"/api/v1/advisors?sort={sort}", headers=cust_headers)
        assert resp.status_code == 200, resp.text


async def test_rating_feeds_ai_matching(client: AsyncClient, engine, admin_token: str) -> None:
    from tests.test_assessments import _opt, _seed_questions

    questions = await _seed_questions(client, admin_token)

    # Two otherwise-identical advisors; only one has a 5-star review.
    adv_a, _, day = await _bookable_advisor(client, engine, "match-a@test.com")
    adv_b, _, _ = await _bookable_advisor(client, engine, "match-b@test.com")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    from sqlalchemy import select

    from app.models.advisor_profile import (
        AdvisorCountryExpertise,
        AdvisorProfile,
        AdvisorVisaSpecialization,
    )

    async with session_factory() as session:
        for adv in (adv_a, adv_b):
            profile = (
                await session.execute(
                    select(AdvisorProfile).where(AdvisorProfile.user_id == uuid.UUID(adv))
                )
            ).scalar_one()
            session.add(AdvisorCountryExpertise(profile_id=profile.id, country_code="GB"))
            session.add(AdvisorVisaSpecialization(profile_id=profile.id, specialization="work"))
        await session.commit()

    _, cust_headers = await _seeker(client)
    booking_id = await _completed_booking(client, engine, cust_headers, adv_a, day)
    await client.post(
        f"/api/v1/bookings/{booking_id}/review",
        json={
            "rating_expertise": 5,
            "rating_communication": 5,
            "rating_professionalism": 5,
            "rating_value": 5,
        },
        headers=cust_headers,
    )

    resp = await client.post(
        "/api/v1/assessments",
        json={"destination_country": "GB", "visa_type": "work"},
        headers=cust_headers,
    )
    assessment_id = resp.json()["data"]["id"]
    resp = await client.post(
        f"/api/v1/assessments/{assessment_id}/answers",
        json={
            "answers": [{"question_id": questions[0]["id"], "option_id": _opt(questions[0], "Yes")}]
        },
        headers=cust_headers,
    )
    matches = resp.json()["data"]["matched_advisors"]
    assert matches[0]["user_id"] == adv_a  # 5-star advisor ranks first
    by_id = {m["user_id"]: m["match_score"] for m in matches}
    assert by_id[adv_a] == by_id[adv_b] + 15.0  # full rating weight difference
