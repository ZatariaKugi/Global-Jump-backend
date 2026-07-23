"""Advisor payout request tests (PRD §3.10) — the manual withdrawal ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.transaction import Transaction, TransactionStatus
from tests.test_bookings import _bookable_advisor, _seeker, _slot_iso

BOOKINGS = "/api/v1/bookings"
PAYOUTS = "/api/v1/advisors/me/payouts"
ADMIN_PAYOUTS = "/api/v1/admin/payouts"


async def _confirmed_booking(client: AsyncClient, engine) -> tuple[str, dict, str]:
    """Returns (booking_id, advisor_headers, advisor_id)."""
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, seeker_headers = await _seeker(client)
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "immigration_specialist",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=seeker_headers,
    )
    assert resp.status_code == 201, resp.text
    booking_id = resp.json()["data"]["id"]
    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    return booking_id, advisor_headers, advisor_id


async def _add_succeeded_transaction(engine, booking_id: str, amount_usd: float = 100.0) -> None:
    commission_usd = round(amount_usd * 0.15, 2)
    tax_usd = round(amount_usd * 0.08, 2)
    advisor_payout_usd = round(amount_usd - commission_usd - tax_usd, 2)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        txn = Transaction(
            booking_id=uuid.UUID(booking_id),
            stripe_checkout_session_id=f"cs_test_{uuid.uuid4().hex[:8]}",
            amount_usd=amount_usd,
            commission_rate=0.15,
            commission_usd=commission_usd,
            tax_rate=0.08,
            tax_usd=tax_usd,
            advisor_payout_usd=advisor_payout_usd,
            status=TransactionStatus.succeeded,
            created_at=datetime.now(UTC),
        )
        session.add(txn)
        await session.commit()


async def test_available_balance_reflects_succeeded_transaction(
    client: AsyncClient, engine
) -> None:
    booking_id, advisor_headers, _ = await _confirmed_booking(client, engine)
    await _add_succeeded_transaction(engine, booking_id, amount_usd=100.0)

    resp = await client.get("/api/v1/advisors/me/earnings", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["available_balance_usd"] == 77.0  # 100 - 15 - 8


async def test_payout_request_rejects_amount_over_balance(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _ = await _confirmed_booking(client, engine)
    await _add_succeeded_transaction(engine, booking_id, amount_usd=100.0)

    resp = await client.post(
        PAYOUTS,
        json={"amount_usd": 1000.0, "method": "bank_transfer"},
        headers=advisor_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "insufficient_balance"


async def test_payout_request_computes_processing_fee(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _ = await _confirmed_booking(client, engine)
    await _add_succeeded_transaction(engine, booking_id, amount_usd=100.0)

    resp = await client.post(
        PAYOUTS,
        json={"amount_usd": 50.0, "method": "paypal", "note": "monthly withdrawal"},
        headers=advisor_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "pending"
    assert data["processing_fee_usd"] == 1.0  # 2% of 50
    assert data["net_amount_usd"] == 49.0

    # Requesting again for the remaining balance succeeds; going over now fails.
    resp = await client.get("/api/v1/advisors/me/earnings", headers=advisor_headers)
    assert resp.json()["data"]["available_balance_usd"] == 27.0  # 77 - 50 reserved

    resp = await client.post(
        PAYOUTS, json={"amount_usd": 30.0, "method": "paypal"}, headers=advisor_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "insufficient_balance"


async def test_seeker_forbidden_from_requesting_payout(client: AsyncClient) -> None:
    _, seeker_headers = await _seeker(client)
    resp = await client.post(
        PAYOUTS, json={"amount_usd": 10.0, "method": "paypal"}, headers=seeker_headers
    )
    assert resp.status_code == 403


async def test_admin_can_complete_payout(client: AsyncClient, engine, admin_token: str) -> None:
    booking_id, advisor_headers, _ = await _confirmed_booking(client, engine)
    await _add_succeeded_transaction(engine, booking_id, amount_usd=100.0)

    resp = await client.post(
        PAYOUTS, json={"amount_usd": 50.0, "method": "bank_transfer"}, headers=advisor_headers
    )
    payout_id = resp.json()["data"]["id"]

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.patch(
        f"{ADMIN_PAYOUTS}/{payout_id}", json={"action": "completed"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "completed"

    # Already-processed requests can't be re-decided.
    resp = await client.patch(
        f"{ADMIN_PAYOUTS}/{payout_id}", json={"action": "rejected"}, headers=admin_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


async def test_admin_reject_releases_balance(client: AsyncClient, engine, admin_token: str) -> None:
    booking_id, advisor_headers, _ = await _confirmed_booking(client, engine)
    await _add_succeeded_transaction(engine, booking_id, amount_usd=100.0)

    resp = await client.post(
        PAYOUTS, json={"amount_usd": 50.0, "method": "bank_transfer"}, headers=advisor_headers
    )
    payout_id = resp.json()["data"]["id"]

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.patch(
        f"{ADMIN_PAYOUTS}/{payout_id}",
        json={"action": "rejected", "rejection_reason": "Suspicious activity"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "rejected"
    assert resp.json()["data"]["rejection_reason"] == "Suspicious activity"

    # The reserved amount is released back to available balance.
    resp = await client.get("/api/v1/advisors/me/earnings", headers=advisor_headers)
    assert resp.json()["data"]["available_balance_usd"] == 77.0


async def test_admin_can_list_and_filter_payouts(
    client: AsyncClient, engine, admin_token: str
) -> None:
    booking_id, advisor_headers, _ = await _confirmed_booking(client, engine)
    await _add_succeeded_transaction(engine, booking_id, amount_usd=100.0)
    await client.post(
        PAYOUTS, json={"amount_usd": 20.0, "method": "stripe"}, headers=advisor_headers
    )

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(f"{ADMIN_PAYOUTS}?status=pending", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1
    assert resp.json()["data"][0]["status"] == "pending"


async def test_advisor_can_only_see_own_payouts(client: AsyncClient, engine) -> None:
    booking_id, advisor_a_headers, _ = await _confirmed_booking(client, engine)
    await _add_succeeded_transaction(engine, booking_id, amount_usd=100.0)
    await client.post(
        PAYOUTS, json={"amount_usd": 20.0, "method": "stripe"}, headers=advisor_a_headers
    )

    _, advisor_b_headers, _ = await _bookable_advisor(client, engine, "advisor-b@test.com")
    resp = await client.get(PAYOUTS, headers=advisor_b_headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == []
