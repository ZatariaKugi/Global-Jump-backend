"""Advisor payment flow tests (PRD §3.10): tax deduction, payment list/detail, invoices.

Stripe calls are mocked throughout — there is no test-mode Stripe sandbox wired into
this suite, and the real (test-mode) API key in .env must never be hit from tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.transaction import Transaction, TransactionStatus
from tests.test_bookings import _bookable_advisor, _seeker, _slot_iso

BOOKINGS = "/api/v1/bookings"
PAYMENTS = "/api/v1/payments"


def _mock_checkout_session(session_id: str = "cs_test_123") -> AsyncMock:
    mock = AsyncMock()
    mock.id = session_id
    mock.url = f"https://checkout.stripe.com/pay/{session_id}"
    return mock


async def _confirmed_booking(client: AsyncClient, engine) -> tuple[str, dict, dict, str, str]:
    """Returns (booking_id, advisor_headers, seeker_headers, advisor_id, seeker_id)."""
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
    booking_id = resp.json()["data"]["id"]

    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=advisor_headers)
    assert resp.status_code == 200, resp.text

    seeker_resp = await client.get("/api/v1/users/me", headers=seeker_headers)
    seeker_id = seeker_resp.json()["data"]["id"]
    return booking_id, advisor_headers, seeker_headers, advisor_id, seeker_id


async def _transaction_id_for_booking(engine, booking_id: str) -> str:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        result = await session.execute(
            Transaction.__table__.select().where(Transaction.booking_id == uuid.UUID(booking_id))
        )
        return str(result.mappings().one()["id"])


async def _succeeded_transaction(
    engine,
    booking_id: str,
    amount_usd: float = 100.0,
    commission_rate: float = 0.15,
    tax_rate: float = 0.08,
    invoice_number: int | None = 1,
    stripe_charge_id: str = "ch_test_fake",
) -> str:
    """Insert a succeeded Transaction directly — bypasses Stripe entirely."""
    commission_usd = round(amount_usd * commission_rate, 2)
    tax_usd = round(amount_usd * tax_rate, 2)
    advisor_payout_usd = round(amount_usd - commission_usd - tax_usd, 2)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        txn = Transaction(
            booking_id=uuid.UUID(booking_id),
            stripe_checkout_session_id=f"cs_test_{uuid.uuid4().hex[:8]}",
            stripe_charge_id=stripe_charge_id,
            amount_usd=amount_usd,
            commission_rate=commission_rate,
            commission_usd=commission_usd,
            tax_rate=tax_rate,
            tax_usd=tax_usd,
            advisor_payout_usd=advisor_payout_usd,
            status=TransactionStatus.succeeded,
            invoice_number=invoice_number,
            created_at=datetime.now(UTC),
        )
        session.add(txn)
        await session.commit()
        await session.refresh(txn)
        return str(txn.id)


# ── Tax deduction on checkout creation ───────────────────────────────────────


async def test_checkout_creation_computes_tax_and_payout(client: AsyncClient, engine) -> None:
    booking_id, _, seeker_headers, _, _ = await _confirmed_booking(client, engine)

    with patch(
        "app.services.payment_service.stripe.checkout.Session.create_async",
        new=AsyncMock(return_value=_mock_checkout_session()),
    ):
        resp = await client.post(
            f"{PAYMENTS}/checkout", json={"booking_id": booking_id}, headers=seeker_headers
        )
    assert resp.status_code == 201, resp.text

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        result = await session.execute(
            Transaction.__table__.select().where(Transaction.booking_id == uuid.UUID(booking_id))
        )
        row = result.mappings().one()
        assert row["tax_rate"] == 0.08
        assert row["tax_usd"] == round(75.0 * 0.08, 2)
        expected_payout = round(75.0 - row["commission_usd"] - row["tax_usd"], 2)
        assert row["advisor_payout_usd"] == expected_payout
        assert row["payment_method"] == "card"


# ── Advisor payment list ─────────────────────────────────────────────────────


async def test_advisor_can_list_own_payments(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, advisor_id, seeker_id = await _confirmed_booking(client, engine)
    await _succeeded_transaction(engine, booking_id)

    resp = await client.get("/api/v1/advisors/me/payments", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["seeker_id"] == seeker_id
    assert data[0]["service_type"] == "consultation_30"
    assert data[0]["status"] == "succeeded"


async def test_earnings_includes_available_balance(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, _, _, _ = await _confirmed_booking(client, engine)
    await _succeeded_transaction(engine, booking_id, amount_usd=100.0)

    resp = await client.get("/api/v1/advisors/me/earnings", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    expected_payout = round(100.0 - 15.0 - 8.0, 2)
    assert data["available_balance_usd"] == expected_payout


# ── Transaction detail (Payment Details modal) ───────────────────────────────


async def test_payment_detail_gated_to_party_or_admin(
    client: AsyncClient, engine, admin_token: str
) -> None:
    booking_id, advisor_headers, seeker_headers, _, _ = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id)

    for headers in (advisor_headers, seeker_headers):
        resp = await client.get(f"{PAYMENTS}/{txn_id}", headers=headers)
        assert resp.status_code == 200, resp.text

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.get(f"{PAYMENTS}/{txn_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text

    _, stranger_headers = await _seeker(client, "stranger@test.com")
    resp = await client.get(f"{PAYMENTS}/{txn_id}", headers=stranger_headers)
    assert resp.status_code == 404


# ── Invoice ───────────────────────────────────────────────────────────────────


async def test_invoice_requires_succeeded_transaction(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, seeker_headers, _, _ = await _confirmed_booking(client, engine)

    with patch(
        "app.services.payment_service.stripe.checkout.Session.create_async",
        new=AsyncMock(return_value=_mock_checkout_session()),
    ):
        resp = await client.post(
            f"{PAYMENTS}/checkout", json={"booking_id": booking_id}, headers=seeker_headers
        )
    assert resp.status_code == 201, resp.text
    txn_id = await _transaction_id_for_booking(engine, booking_id)

    resp = await client.get(f"{PAYMENTS}/{txn_id}/invoice", headers=advisor_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "not_paid"


async def test_invoice_returns_correct_totals(client: AsyncClient, engine) -> None:
    booking_id, advisor_headers, seeker_headers, _, _ = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id, amount_usd=200.0, invoice_number=42)

    resp = await client.get(f"{PAYMENTS}/{txn_id}/invoice", headers=seeker_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["invoice_number"] == "00000042"
    assert data["total_usd"] == 200.0
    assert len(data["line_items"]) == 2
    assert data["status"] == "succeeded"
    assert data["refunded_amount_usd"] is None


async def test_invoice_reflects_refund_summary(
    client: AsyncClient, engine, admin_token: str
) -> None:
    booking_id, _, seeker_headers, _, _ = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id, amount_usd=200.0, invoice_number=7)

    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    with patch("app.services.payment_service.stripe.Refund.create_async", new=AsyncMock()):
        resp = await client.post(
            f"/api/v1/admin/payments/{txn_id}/refund",
            json={"reason": "not satisfied", "amount_usd": 50.0},
            headers=admin_headers,
        )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"{PAYMENTS}/{txn_id}/invoice", headers=seeker_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "partially_refunded"
    assert data["refunded_amount_usd"] == 50.0
    assert data["refund_reason"] == "not satisfied"


# ── Receipt email ─────────────────────────────────────────────────────────────


async def test_payment_receipt_email_sent_on_successful_payment(
    client: AsyncClient, engine
) -> None:
    booking_id, _, seeker_headers, _, _ = await _confirmed_booking(client, engine)

    session = _mock_checkout_session()
    with patch(
        "app.services.payment_service.stripe.checkout.Session.create_async",
        new=AsyncMock(return_value=session),
    ):
        resp = await client.post(
            f"{PAYMENTS}/checkout", json={"booking_id": booking_id}, headers=seeker_headers
        )
    assert resp.status_code == 201, resp.text

    import json as _json

    webhook_event = {
        "id": "evt_test_receipt",
        "type": "checkout.session.completed",
        "data": {"object": {"id": session.id, "payment_intent": None}},
    }
    with patch(
        "app.services.payment_service.email_service.send_payment_receipt_email",
        new=AsyncMock(),
    ) as mock_send:
        resp = await client.post(f"{PAYMENTS}/webhook", content=_json.dumps(webhook_event))
    assert resp.status_code == 200, resp.text

    mock_send.assert_awaited_once()
    assert mock_send.call_args.args[0] == "cust@test.com"
    assert mock_send.call_args.kwargs["service_type"] == "consultation_30"
