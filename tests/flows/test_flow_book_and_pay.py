"""Phase 2 (#185) · flows 1 and 2: a seeker books and pays; an advisor cancels and refunds.

Money on a $100 consultation with the default settings (15 % commission, Stripe fee $3.20):
seeker pays 100 · platform keeps 15 · Stripe's fee comes out of the advisor · advisor nets 81.80.
"""

from __future__ import annotations

import pytest

from tests.flows import booking_helpers as b
from tests.flows import helpers as h
from tests.flows.conftest import World

pytestmark = pytest.mark.asyncio


async def _ready_advisor_with_a_priced_service(world: World) -> str:
    service_id = await b.create_global_service(world, "Consultation")
    await b.offer_service(world, world.advisor, service_id, price_usd=100, duration_minutes=30)
    await b.open_all_week(world, world.advisor)
    await h.connect_zoom(world, world.advisor)
    return service_id


async def test_seeker_books_pays_and_both_sides_see_the_money(world: World) -> None:
    service_id = await _ready_advisor_with_a_priced_service(world)
    start = await b.first_free_slot(world, world.seeker, world.advisor_id)

    booking = await b.book(
        world, world.seeker, world.advisor_id, service_id, start, note="Study visa"
    )
    assert booking["status"] == "pending" and booking["payment_status"] == "unpaid"
    assert float(booking["price_usd"]) == 100 and booking["duration_minutes"] == 30

    await b.pay(world, world.seeker, booking["id"], stripe_fee_cents=320)

    paid = await b.get_booking(world, world.seeker, booking["id"])
    assert paid["status"] == "confirmed" and paid["payment_status"] == "paid"
    # a Zoom meeting is provisioned once the booking is paid; its link only shows near start
    assert paid["meeting_join_url"] is None
    details = await b.get_booking_details(world, world.seeker, booking["id"])
    assert details["meeting"] and details["meeting"]["passcode"] == "123456"
    assert details["meeting"]["join_url"] is None  # shown only when the meeting is joinable
    assert world.zoom.called("create_scheduled_meeting")

    # the seeker's side
    rows = await b.seeker_payments(world, world.seeker)
    assert (
        len(rows) == 1 and float(rows[0]["amount_usd"]) == 100 and rows[0]["status"] == "succeeded"
    )
    invoice = h.body(
        await world.client.get(f"/api/v1/payments/{rows[0]['id']}/invoice", headers=world.seeker)
    )
    assert float(invoice["total_usd"]) == 100 and float(invoice["tax_usd"]) == 0
    assert invoice["to_email"] == "seeker@flows-world.com"

    # the advisor's side: commission and Stripe's fee come off the price
    earnings = h.body(await world.client.get("/api/v1/advisors/me/earnings", headers=world.advisor))
    txn = earnings["transactions"][0]
    assert float(txn["amount_usd"]) == 100 and float(txn["commission_usd"]) == 15
    assert float(earnings["total_commission_paid_usd"]) == 15
    assert float(earnings["total_stripe_fee_usd"]) == pytest.approx(3.20, abs=0.01)
    assert float(earnings["total_earned_usd"]) == pytest.approx(81.80, abs=0.01)
    assert len(world.stripe.called("Transfer.create_reversal")) == 1, (
        "Stripe's fee is recovered from the advisor"
    )

    # the platform's side: revenue is the commission, never the price
    summary = await b.admin_summary(world)
    assert float(summary["total_commission_usd"]) == 15
    assert float(summary["total_stripe_fee_usd"]) == pytest.approx(3.20, abs=0.01)
    txns = await b.admin_transactions(world)
    assert len(txns) == 1 and txns[0]["status"] == "succeeded"

    # both were told
    assert "booking_requested" in await b.notifications(world, world.advisor)
    seeker_types = await b.notifications(world, world.seeker)
    assert "payment_succeeded" in seeker_types or "booking_confirmed" in seeker_types


async def test_advisor_cancel_is_locked_for_two_minutes_then_refunds_the_advisor_share(
    world: World,
) -> None:
    service_id = await _ready_advisor_with_a_priced_service(world)
    start = await b.first_free_slot(world, world.seeker, world.advisor_id)
    booking = await b.book(world, world.seeker, world.advisor_id, service_id, start)
    await b.pay(world, world.seeker, booking["id"], stripe_fee_cents=320)

    locked = await world.client.post(
        f"/api/v1/bookings/{booking['id']}/cancel", json={"reason": "Clash"}, headers=world.advisor
    )
    assert locked.status_code == 400 and locked.json()["error"]["code"] == "cancel_cooldown"

    await b.set_paid_at(world, booking["id"], minutes_ago=5)
    cancelled = h.body(
        await world.client.post(
            f"/api/v1/bookings/{booking['id']}/cancel",
            json={"reason": "Clash"},
            headers=world.advisor,
        )
    )
    assert cancelled["status"] == "cancelled"

    # the refund went to Stripe for the advisor's share: price − commission − Stripe's fee
    refunds = world.stripe.called("Refund.create")
    assert len(refunds) == 1 and refunds[0]["amount"] == 8180
    rows = await b.seeker_payments(world, world.seeker)
    assert rows[0]["status"] in ("refunded", "partially_refunded")
    assert float(rows[0]["refunded_amount_usd"]) == pytest.approx(81.80, abs=0.01)
    assert "booking_cancelled" in await b.notifications(world, world.seeker)
    assert world.zoom.called("delete_meeting"), "the Zoom meeting is removed with the booking"
