"""Phase 2 (#185) · flow 3: the consultation from the advisor's chair — and the seeker's reschedule.

reschedule inside the window · reject an unpaid request (nothing to refund) · hold the session ·
complete it · no-show on another · the money agrees on every screen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.flows import booking_helpers as b
from tests.flows import helpers as h
from tests.flows.conftest import World

pytestmark = pytest.mark.asyncio


async def _setup(world: World) -> str:
    service_id = await b.create_global_service(world, "Consultation")
    await b.offer_service(world, world.advisor, service_id, price_usd=100, duration_minutes=30)
    await b.open_all_week(world, world.advisor)
    await h.connect_zoom(world, world.advisor)
    return service_id


async def _move_into_the_past(world: World, booking_id: str, hours_ago: int = 2) -> None:
    """The session time has come and gone (the API refuses to complete a future session)."""
    from app.models.booking import Booking

    async with world.Session() as s:
        booking = await s.get(Booking, uuid.UUID(booking_id))
        assert booking is not None
        booking.scheduled_start = datetime.now(UTC) - timedelta(hours=hours_ago)
        booking.scheduled_end = booking.scheduled_start + timedelta(
            minutes=booking.duration_minutes
        )
        await s.commit()


async def test_seeker_reschedules_a_paid_consultation_inside_the_window(world: World) -> None:
    service_id = await _setup(world)
    start = await b.first_free_slot(world, world.seeker, world.advisor_id)
    booking = await b.book(world, world.seeker, world.advisor_id, service_id, start)
    await b.pay(world, world.seeker, booking["id"])

    day_after = (datetime.now(UTC) + timedelta(days=3)).date()
    slots = h.body(
        await world.client.get(
            f"/api/v1/advisors/{world.advisor_id}/availability",
            params={
                "date_from": day_after.isoformat(),
                "date_to": day_after.isoformat(),
                "duration_minutes": 30,
            },
            headers=world.seeker,
        )
    )
    new_start = str(slots[0].get("start_utc") or slots[0].get("start"))
    moved = h.body(
        await world.client.post(
            f"/api/v1/bookings/{booking['id']}/reschedule",
            json={"scheduled_start": new_start},
            headers=world.seeker,
        )
    )
    assert moved["status"] == "confirmed" and moved["scheduled_start"].startswith(new_start[:16])
    assert world.zoom.called("update_scheduled_meeting"), "the Zoom meeting moves with the booking"
    assert "booking_rescheduled" in await b.notifications(world, world.advisor)


async def test_advisor_rejects_an_unpaid_request_and_nothing_is_refunded(world: World) -> None:
    service_id = await _setup(world)
    start = await b.first_free_slot(world, world.seeker, world.advisor_id)
    booking = await b.book(world, world.seeker, world.advisor_id, service_id, start)

    rejected = h.body(
        await world.client.post(
            f"/api/v1/bookings/{booking['id']}/reject",
            json={"reason": "Outside my expertise"},
            headers=world.advisor,
        )
    )
    assert rejected["status"] == "rejected"
    assert world.stripe.called("Refund.create") == [], "nothing was paid, nothing to refund"
    assert "booking_rejected" in await b.notifications(world, world.seeker)
    assert await b.seeker_payments(world, world.seeker) == []


async def test_advisor_completes_a_held_session_and_marks_another_no_show(world: World) -> None:
    service_id = await _setup(world)

    # session one: held and completed
    start = await b.first_free_slot(world, world.seeker, world.advisor_id)
    first = await b.book(world, world.seeker, world.advisor_id, service_id, start)
    await b.pay(world, world.seeker, first["id"])
    too_early = await world.client.post(
        f"/api/v1/bookings/{first['id']}/complete", headers=world.advisor
    )
    assert too_early.status_code == 400 and too_early.json()["error"]["code"] == "invalid_state"
    await _move_into_the_past(world, first["id"])
    done = h.body(
        await world.client.post(f"/api/v1/bookings/{first['id']}/complete", headers=world.advisor)
    )
    assert done["status"] == "completed"
    assert "booking_completed" in await b.notifications(world, world.seeker)

    # session two: the seeker never showed
    day_after = (datetime.now(UTC) + timedelta(days=3)).date()
    slots = h.body(
        await world.client.get(
            f"/api/v1/advisors/{world.advisor_id}/availability",
            params={
                "date_from": day_after.isoformat(),
                "date_to": day_after.isoformat(),
                "duration_minutes": 30,
            },
            headers=world.seeker,
        )
    )
    second = await b.book(
        world,
        world.seeker,
        world.advisor_id,
        service_id,
        str(slots[0].get("start_utc") or slots[0]["start"]),
    )
    await b.pay(world, world.seeker, second["id"])
    await _move_into_the_past(world, second["id"])
    missed = h.body(
        await world.client.post(f"/api/v1/bookings/{second['id']}/no-show", headers=world.advisor)
    )
    assert missed["status"] == "no_show"

    # the money: two paid sessions, both earned, none refunded
    earnings = h.body(await world.client.get("/api/v1/advisors/me/earnings", headers=world.advisor))
    assert (
        float(earnings["total_gross_revenue_usd"]) == 200
        and float(earnings["total_refunded_usd"]) == 0
    )
    summary = await b.admin_summary(world)
    assert float(summary["total_commission_usd"]) == 30
    clients = h.body(await world.client.get("/api/v1/advisors/me/clients", headers=world.advisor))
    assert [c["email"] for c in clients] == ["seeker@flows-world.com"]
