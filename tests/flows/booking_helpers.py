"""Steps around a consultation: catalogue, prices, availability, booking, paying, reading money."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.flows.conftest import World
from tests.flows.helpers import body

ALL_DAY_UTC = [
    {"weekday": d, "start_time": "00:00:00", "end_time": "23:59:00", "timezone": "UTC"}
    for d in range(7)
]


async def create_global_service(world: World, name: str = "Consultation") -> str:
    """Admin adds a service to the catalogue every advisor can offer."""
    row = body(
        await world.client.post(
            "/api/v1/admin/offered_services", json={"name": name}, headers=world.admin
        )
    )
    return str(row.get("service_id") or row["id"])


async def offer_service(
    world: World,
    headers: dict[str, str],
    service_id: str,
    *,
    price_usd: float = 100,
    duration_minutes: int = 30,
) -> None:
    body(
        await world.client.put(
            "/api/v1/advisors/me/offered_services",
            json={
                "services": [
                    {
                        "service_id": service_id,
                        "price_usd": price_usd,
                        "duration_minutes": duration_minutes,
                    }
                ]
            },
            headers=headers,
        )
    )


async def open_all_week(world: World, headers: dict[str, str]) -> None:
    body(
        await world.client.put(
            "/api/v1/advisors/me/availability", json={"slots": ALL_DAY_UTC}, headers=headers
        )
    )


async def first_free_slot(
    world: World,
    headers: dict[str, str],
    advisor_id: str | uuid.UUID,
    *,
    duration_minutes: int = 30,
) -> str:
    """The first bookable UTC start two days from now, as the seeker sees it."""
    day = (datetime.now(UTC) + timedelta(days=2)).date()
    slots = body(
        await world.client.get(
            f"/api/v1/advisors/{advisor_id}/availability",
            params={
                "date_from": day.isoformat(),
                "date_to": day.isoformat(),
                "duration_minutes": duration_minutes,
            },
            headers=headers,
        )
    )
    assert slots, "the advisor has no free slot two days from now"
    first = slots[0]
    return str(first.get("start_utc") or first.get("start"))


async def book(
    world: World,
    headers: dict[str, str],
    advisor_id: str | uuid.UUID,
    service_id: str,
    scheduled_start: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    return body(
        await world.client.post(
            "/api/v1/bookings",
            json={
                "advisor_id": str(advisor_id),
                "service_id": service_id,
                "scheduled_start": scheduled_start,
                "seeker_note": note,
            },
            headers=headers,
        )
    )


async def pay(
    world: World, headers: dict[str, str], booking_id: str, *, stripe_fee_cents: int = 320
) -> dict[str, Any]:
    """Checkout, then the payment completes the way Stripe completes it: a signed webhook."""
    checkout = body(
        await world.client.post(
            "/api/v1/payments/checkout", json={"booking_id": booking_id}, headers=headers
        )
    )
    assert checkout["checkout_url"].startswith("https://checkout.stripe.test/")
    event = world.stripe.complete_checkout(checkout["session_id"], fee_cents=stripe_fee_cents)
    r = await world.stripe.deliver(world.client, event)
    assert r.status_code == 200, r.text
    return checkout


async def get_booking(world: World, headers: dict[str, str], booking_id: str) -> dict[str, Any]:
    return body(await world.client.get(f"/api/v1/bookings/{booking_id}", headers=headers))


async def get_booking_details(
    world: World, headers: dict[str, str], booking_id: str
) -> dict[str, Any]:
    return body(await world.client.get(f"/api/v1/bookings/{booking_id}/details", headers=headers))


async def seeker_payments(world: World, headers: dict[str, str]) -> list[dict[str, Any]]:
    return body(await world.client.get("/api/v1/payments/history", headers=headers))


async def admin_transactions(world: World) -> list[dict[str, Any]]:
    return body(
        await world.client.get(
            "/api/v1/admin/payments", params={"page_size": 50}, headers=world.admin
        )
    )


async def admin_summary(world: World) -> dict[str, Any]:
    return body(await world.client.get("/api/v1/admin/payments/summary", headers=world.admin))


async def notifications(world: World, headers: dict[str, str]) -> list[str]:
    rows = body(
        await world.client.get("/api/v1/notifications", params={"page_size": 100}, headers=headers)
    )
    return [str(r["type"]) for r in rows]


async def set_paid_at(world: World, booking_id: str, minutes_ago: int) -> None:
    """Move the payment time back so the two-minute cancel lock has passed."""
    from app.models.booking import Booking

    async with world.Session() as s:
        booking = await s.get(Booking, uuid.UUID(booking_id))
        assert booking is not None
        booking.paid_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        await s.commit()
