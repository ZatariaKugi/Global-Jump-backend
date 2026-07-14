"""Calendar-view backend: date-range/service-type booking filters, advisor-initiated
bookings, and the existing-clients picker."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient

from tests.test_bookings import _bookable_advisor, _seeker, _slot_iso

BOOKINGS = "/api/v1/bookings"
ADVISOR_BOOKINGS = "/api/v1/advisors/me/bookings"
CLIENTS = "/api/v1/advisors/me/clients"


async def _user_id(client: AsyncClient, headers: dict) -> str:
    resp = await client.get("/api/v1/users/me", headers=headers)
    return str(resp.json()["data"]["id"])


# ── Date-range / service-type filters ────────────────────────────────────────


async def test_date_range_filter_scopes_bookings(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)

    near_day = day
    far_day = day + timedelta(days=14)
    for d in (near_day, far_day):
        resp = await client.post(
            BOOKINGS,
            json={
                "advisor_id": advisor_id,
                "service_type": "consultation_30",
                "scheduled_start": _slot_iso(d, 10),
            },
            headers=cust_headers,
        )
        assert resp.status_code == 201, resp.text

    resp = await client.get(
        f"{BOOKINGS}?date_from={near_day}&date_to={near_day}", headers=cust_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["scheduled_start"].startswith(str(near_day))


async def test_service_type_filter_narrows_bookings(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    resp = await client.patch(
        "/api/v1/advisors/me/profile",
        json={
            "services": [
                {"service_type": "consultation_30", "duration_minutes": 30, "price_usd": 75.0},
                {"service_type": "document_review", "duration_minutes": 30, "price_usd": 50.0},
            ]
        },
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text

    _, cust_headers = await _seeker(client)
    for service_type, hour in (("consultation_30", 9), ("document_review", 11)):
        resp = await client.post(
            BOOKINGS,
            json={
                "advisor_id": advisor_id,
                "service_type": service_type,
                "scheduled_start": _slot_iso(day, hour),
            },
            headers=cust_headers,
        )
        assert resp.status_code == 201, resp.text

    resp = await client.get(f"{BOOKINGS}?service_type=document_review", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["service_type"] == "document_review"


# ── Advisor-initiated booking creation ───────────────────────────────────────


async def test_advisor_can_create_booking_for_client(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    seeker_id = await _user_id(client, cust_headers)

    resp = await client.post(
        ADVISOR_BOOKINGS,
        json={
            "seeker_id": seeker_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=advisor_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "confirmed"  # advisor-initiated: no accept step needed
    assert data["seeker_id"] == seeker_id
    assert data["advisor_id"] == advisor_id


async def test_advisor_booking_creation_conflicts_with_existing_booking(
    client: AsyncClient, engine
) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    seeker_id = await _user_id(client, cust_headers)

    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=cust_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        ADVISOR_BOOKINGS,
        json={
            "seeker_id": seeker_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=advisor_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "slot_unavailable"


async def test_advisor_booking_creation_rejects_non_seeker(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    resp = await client.post(
        ADVISOR_BOOKINGS,
        json={
            "seeker_id": advisor_id,  # the advisor's own id — not a seeker
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=advisor_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_booking"


async def test_seeker_forbidden_from_advisor_booking_creation(client: AsyncClient, engine) -> None:
    advisor_id, _, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    seeker_id = await _user_id(client, cust_headers)

    resp = await client.post(
        ADVISOR_BOOKINGS,
        json={
            "seeker_id": seeker_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=cust_headers,
    )
    assert resp.status_code == 403


# ── Existing-clients picker ───────────────────────────────────────────────────


async def test_list_clients_returns_only_this_advisors_seekers(client: AsyncClient, engine) -> None:
    advisor_a_id, advisor_a_headers, day_a = await _bookable_advisor(
        client, engine, "advA@test.com"
    )
    advisor_b_id, advisor_b_headers, day_b = await _bookable_advisor(
        client, engine, "advB@test.com"
    )

    _, seeker_a_headers = await _seeker(client, "clientA@test.com")
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_a_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day_a, 10),
        },
        headers=seeker_a_headers,
    )
    assert resp.status_code == 201, resp.text

    _, seeker_b_headers = await _seeker(client, "clientB@test.com")
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_b_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day_b, 10),
        },
        headers=seeker_b_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(CLIENTS, headers=advisor_a_headers)
    assert resp.status_code == 200, resp.text
    emails = {c["email"] for c in resp.json()["data"]}
    assert emails == {"clientA@test.com"}


async def test_list_clients_search_filters_by_name(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)

    await client.post(
        "/api/v1/auth/register",
        json={"email": "henry@test.com", "password": "custpass123", "full_name": "Henry Client"},
    )
    login = await client.post(
        "/api/v1/auth/login", data={"username": "henry@test.com", "password": "custpass123"}
    )
    henry_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=henry_headers,
    )
    assert resp.status_code == 201, resp.text

    _, other_headers = await _seeker(client, "other-client@test.com")
    resp = await client.post(
        BOOKINGS,
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 11),
        },
        headers=other_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(f"{CLIENTS}?q=Henry", headers=advisor_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["full_name"] == "Henry Client"


async def test_list_clients_empty_for_advisor_with_no_bookings(client: AsyncClient, engine) -> None:
    _, advisor_headers, _ = await _bookable_advisor(client, engine)
    resp = await client.get(CLIENTS, headers=advisor_headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == []
