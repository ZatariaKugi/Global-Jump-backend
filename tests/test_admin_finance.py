"""Admin Finance Management tests (PRD §4.5): list/detail/timeline, partial refunds."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from tests.test_advisor_search import _make_advisor
from tests.test_bookings import _next_weekday, _slot_iso
from tests.test_payments import (
    PAYMENTS,
    _confirmed_booking,
    _mock_checkout_session,
    _seeker,
    _succeeded_transaction,
    _transaction_id_for_booking,
)

FINANCE = "/api/v1/admin/payments"
BOOKINGS = "/api/v1/bookings"
AVAIL = "/api/v1/advisors/me/availability"


async def _named_booking(
    client: AsyncClient,
    engine,
    seeker_email: str,
    seeker_name: str,
    advisor_email: str,
    advisor_name: str,
) -> str:
    """A confirmed booking between a seeker and advisor with chosen display names."""
    advisor_id, advisor_token = await _make_advisor(
        client,
        engine,
        advisor_email,
        advisor_name,
        {
            "services": [
                {"service_type": "consultation_30", "duration_minutes": 30, "price_usd": 75.0}
            ]
        },
    )
    advisor_headers = {"Authorization": f"Bearer {advisor_token}"}
    resp = await client.put(
        AVAIL,
        json={
            "slots": [{"weekday": 2, "start_time": "00:00", "end_time": "23:30", "timezone": "UTC"}]
        },
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text

    await client.post(
        "/api/v1/auth/register",
        json={"email": seeker_email, "password": "custpass123", "full_name": seeker_name},
    )
    login = await client.post(
        "/api/v1/auth/login", data={"username": seeker_email, "password": "custpass123"}
    )
    seeker_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(_next_weekday(2), 10),
        },
        headers=seeker_headers,
    )
    assert resp.status_code == 201, resp.text
    booking_id = resp.json()["data"]["id"]
    resp = await client.post(f"{BOOKINGS}/{booking_id}/accept", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    return str(booking_id)


# ── List + filters ────────────────────────────────────────────────────────────


async def test_admin_can_list_payments_filtered_by_status(
    client: AsyncClient, engine, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    booking_id, *_ = await _confirmed_booking(client, engine)
    await _succeeded_transaction(engine, booking_id)

    resp = await client.get(f"{FINANCE}?status=succeeded", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1
    assert resp.json()["data"][0]["status"] == "succeeded"

    resp = await client.get(f"{FINANCE}?status=refunded", headers=admin_headers)
    assert resp.json()["data"] == []


async def test_admin_can_search_payments_by_name(
    client: AsyncClient, engine, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    booking_a = await _named_booking(
        client, engine, "alice@test.com", "Alice Anderson", "adv-a@test.com", "Advisor One"
    )
    booking_b = await _named_booking(
        client, engine, "bob@test.com", "Bob Baker", "adv-b@test.com", "Advisor Two"
    )
    await _succeeded_transaction(engine, booking_a, invoice_number=1)
    await _succeeded_transaction(engine, booking_b, invoice_number=2)

    resp = await client.get(f"{FINANCE}?search=Alice", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["seeker_name"] == "Alice Anderson"
    assert data[0]["advisor_name"] == "Advisor One"
    assert data[0]["service_type"] == "consultation_30"


async def test_non_admin_forbidden_from_finance_list(client: AsyncClient) -> None:
    token, _ = await _seeker(client)
    resp = await client.get(FINANCE, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# ── Detail ────────────────────────────────────────────────────────────────────


async def test_admin_payment_detail_includes_parties(
    client: AsyncClient, engine, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    booking_id, _, _, advisor_id, seeker_id = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id)

    resp = await client.get(f"{FINANCE}/{txn_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["seeker_id"] == seeker_id
    assert data["advisor_id"] == advisor_id
    assert data["seeker_name"] == "Seeker"
    assert data["advisor_name"] == "Bookable Advisor"


# ── Timeline & Logs ───────────────────────────────────────────────────────────


async def test_timeline_records_checkout_and_refund_lifecycle(
    client: AsyncClient, engine, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
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
    txn_id = await _transaction_id_for_booking(engine, booking_id)

    resp = await client.get(f"{FINANCE}/{txn_id}/timeline", headers=admin_headers)
    assert [e["event_type"] for e in resp.json()["data"]] == ["initiated"]

    webhook_event = {
        "id": "evt_test_1",
        "type": "checkout.session.completed",
        "data": {"object": {"id": session.id, "payment_intent": "pi_test_123"}},
    }
    import json as _json

    with patch(
        "app.services.payment_service.stripe.PaymentIntent.retrieve_async",
        new=AsyncMock(return_value={"latest_charge": {"id": "ch_test_abc"}}),
    ):
        resp = await client.post(f"{PAYMENTS}/webhook", content=_json.dumps(webhook_event))
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"{FINANCE}/{txn_id}/timeline", headers=admin_headers)
    events = [e["event_type"] for e in resp.json()["data"]]
    assert events == [
        "initiated",
        "authorized",
        "completed",
        "invoice_generated",
        "receipt_sent",
        "closed",
    ]

    with patch("app.services.payment_service.stripe.Refund.create_async", new=AsyncMock()):
        resp = await client.post(
            f"{FINANCE}/{txn_id}/refund", json={"reason": "customer request"}, headers=admin_headers
        )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"{FINANCE}/{txn_id}/timeline", headers=admin_headers)
    events = [e["event_type"] for e in resp.json()["data"]]
    assert events[-2:] == ["refunded", "closed"]


# ── Refunds ───────────────────────────────────────────────────────────────────


async def test_partial_refund_sets_partially_refunded_status(
    client: AsyncClient, engine, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    booking_id, *_ = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id, amount_usd=100.0)

    with patch(
        "app.services.payment_service.stripe.Refund.create_async", new=AsyncMock()
    ) as mock_refund:
        resp = await client.post(
            f"{FINANCE}/{txn_id}/refund",
            json={"reason": "partial", "amount_usd": 40.0},
            headers=admin_headers,
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "partially_refunded"
    assert data["refunded_amount_usd"] == 40.0
    assert mock_refund.call_args.kwargs["amount"] == 4000


async def test_full_refund_still_sets_refunded_status(
    client: AsyncClient, engine, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    booking_id, *_ = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id, amount_usd=100.0)

    with patch("app.services.payment_service.stripe.Refund.create_async", new=AsyncMock()):
        resp = await client.post(f"{FINANCE}/{txn_id}/refund", json={}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "refunded"
    assert data["refunded_amount_usd"] == 100.0


async def test_refund_amount_exceeding_total_rejected(
    client: AsyncClient, engine, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    booking_id, *_ = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id, amount_usd=100.0)

    with patch("app.services.payment_service.stripe.Refund.create_async", new=AsyncMock()):
        resp = await client.post(
            f"{FINANCE}/{txn_id}/refund",
            json={"amount_usd": 150.0},
            headers=admin_headers,
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "refund_amount_too_large"


async def test_non_admin_forbidden_from_refund(client: AsyncClient, engine) -> None:
    booking_id, _, seeker_headers, _, _ = await _confirmed_booking(client, engine)
    txn_id = await _succeeded_transaction(engine, booking_id)
    resp = await client.post(f"{FINANCE}/{txn_id}/refund", json={}, headers=seeker_headers)
    assert resp.status_code == 403
