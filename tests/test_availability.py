"""Advisor availability calendar tests (epic #8, PRD §3.6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient

from tests.test_advisor_search import _make_advisor

AVAIL = "/api/v1/advisors/me/availability"


def _next_weekday(weekday: int) -> date:
    """Next future date falling on the given weekday (0=Mon)."""
    today = datetime.now(UTC).date()
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


async def _setup_advisor(client: AsyncClient, engine, email: str = "avail@test.com"):
    """Approved advisor with a service offering; returns (advisor_id, token, headers)."""
    advisor_id, token = await _make_advisor(
        client,
        engine,
        email,
        "Avail Advisor",
        {
            "services": [
                {
                    "service_type": "immigration_specialist",
                    "duration_minutes": 30,
                    "price_usd": 50.0,
                }
            ]
        },
    )
    return advisor_id, token, {"Authorization": f"Bearer {token}"}


async def test_replace_and_get_weekly_slots(client: AsyncClient, engine) -> None:
    _, _, headers = await _setup_advisor(client, engine)

    resp = await client.put(
        AVAIL,
        json={
            "slots": [
                {"weekday": 0, "start_time": "09:00", "end_time": "12:00", "timezone": "UTC"},
                {"weekday": 2, "start_time": "14:00", "end_time": "16:00", "timezone": "UTC"},
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 2

    # Replace shrinks to one slot.
    resp = await client.put(
        AVAIL,
        json={
            "slots": [{"weekday": 4, "start_time": "10:00", "end_time": "11:00", "timezone": "UTC"}]
        },
        headers=headers,
    )
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["weekday"] == 4

    resp = await client.get(AVAIL, headers=headers)
    assert len(resp.json()["data"]) == 1


async def test_invalid_timezone_and_times_rejected(client: AsyncClient, engine) -> None:
    _, _, headers = await _setup_advisor(client, engine)

    resp = await client.put(
        AVAIL,
        json={
            "slots": [
                {
                    "weekday": 0,
                    "start_time": "09:00",
                    "end_time": "12:00",
                    "timezone": "Not/AZone",
                }
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 422

    resp = await client.put(
        AVAIL,
        json={
            "slots": [{"weekday": 0, "start_time": "12:00", "end_time": "09:00", "timezone": "UTC"}]
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_free_slots_timezone_conversion(client: AsyncClient, engine) -> None:
    advisor_id, _, headers = await _setup_advisor(client, engine)

    # Karachi is UTC+5 year-round: 10:00–12:00 local = 05:00–07:00 UTC.
    target = _next_weekday(1)  # next Tuesday
    await client.put(
        AVAIL,
        json={
            "slots": [
                {
                    "weekday": 1,
                    "start_time": "10:00",
                    "end_time": "12:00",
                    "timezone": "Asia/Karachi",
                }
            ]
        },
        headers=headers,
    )

    resp = await client.get(
        f"/api/v1/advisors/{advisor_id}/availability"
        f"?date_from={target}&date_to={target}&duration_minutes=60",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    slots = resp.json()["data"]
    assert len(slots) == 2
    assert slots[0]["start_utc"].startswith(f"{target}T05:00")
    assert slots[1]["start_utc"].startswith(f"{target}T06:00")


async def test_override_blocks_day(client: AsyncClient, engine) -> None:
    advisor_id, _, headers = await _setup_advisor(client, engine)
    target = _next_weekday(1)

    await client.put(
        AVAIL,
        json={
            "slots": [{"weekday": 1, "start_time": "09:00", "end_time": "10:00", "timezone": "UTC"}]
        },
        headers=headers,
    )

    resp = await client.post(
        f"{AVAIL}/overrides",
        json={"date": str(target), "reason": "Holiday"},
        headers=headers,
    )
    assert resp.status_code == 201
    override_id = resp.json()["data"]["id"]

    resp = await client.get(
        f"/api/v1/advisors/{advisor_id}/availability"
        f"?date_from={target}&date_to={target}&duration_minutes=30",
        headers=headers,
    )
    assert resp.json()["data"] == []

    # Deleting the override restores the slots.
    resp = await client.delete(f"{AVAIL}/overrides/{override_id}", headers=headers)
    assert resp.status_code == 204
    resp = await client.get(
        f"/api/v1/advisors/{advisor_id}/availability"
        f"?date_from={target}&date_to={target}&duration_minutes=30",
        headers=headers,
    )
    assert len(resp.json()["data"]) == 2


async def test_availability_requires_verified_advisor(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "c@test.com", "password": "custpass123", "full_name": "C"},
    )
    login = await client.post(
        "/api/v1/auth/login", data={"username": "c@test.com", "password": "custpass123"}
    )
    token = login.json()["access_token"]
    resp = await client.get(AVAIL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_free_slots_invalid_range(client: AsyncClient, engine) -> None:
    advisor_id, _, headers = await _setup_advisor(client, engine)
    today = datetime.now(UTC).date()
    resp = await client.get(
        f"/api/v1/advisors/{advisor_id}/availability"
        f"?date_from={today}&date_to={today - timedelta(days=1)}",
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_range"
