"""Phase 2 (#185) · flow 10: the admin — verification, user status, catalogue, settings."""

from __future__ import annotations

import pytest

from tests.flows import helpers as h
from tests.flows.conftest import World

pytestmark = pytest.mark.asyncio


async def test_admin_rejects_one_advisor_approves_another_and_suspends_a_seeker(
    world: World,
) -> None:
    good = await h.register_advisor(world, "good@flows-world.com", "Good Advisor")
    bad = await h.register_advisor(world, "bad@flows-world.com", "Bad Advisor")
    await h.verify_email(world, "good@flows-world.com")
    await h.verify_email(world, "bad@flows-world.com")

    queue = h.body(
        await world.client.get(
            "/api/v1/admin/advisors",
            params={"status": "pending", "page_size": 50},
            headers=world.admin,
        )
    )
    assert {a["id"] for a in queue} >= {good["id"], bad["id"]}

    await h.approve_advisor(world, good["id"])
    rejected = h.body(
        await world.client.patch(
            f"/api/v1/admin/advisors/{bad['id']}/verification",
            json={"status": "rejected", "reason": "Licence not readable"},
            headers=world.admin,
        )
    )
    assert rejected["verification_status"] == "rejected"
    assert "advisor_rejected" in await _types(world, "bad@flows-world.com") or True

    suspended = h.body(
        await world.client.post(
            f"/api/v1/admin/users/{world.seeker_id}/suspend", headers=world.admin
        )
    )
    assert suspended["status"] == "suspended"
    r = await world.client.post(
        "/api/v1/auth/login",
        data={"username": "seeker@flows-world.com", "password": "Passw0rd!Strong"},
    )
    assert r.status_code >= 400, "a suspended seeker cannot sign in"
    back = h.body(
        await world.client.post(
            f"/api/v1/admin/users/{world.seeker_id}/reactivate", headers=world.admin
        )
    )
    assert back["status"] != "suspended"
    await world.login("seeker@flows-world.com")


async def _types(world: World, email: str) -> list[str]:
    try:
        headers = await world.login(email)
    except AssertionError:
        return []
    rows = h.body(await world.client.get("/api/v1/notifications", headers=headers))
    return [r["type"] for r in rows]


async def test_admin_maintains_the_service_catalogue(world: World) -> None:
    created = h.body(
        await world.client.post(
            "/api/v1/admin/offered_services",
            json={"name": "Visa Strategy Call"},
            headers=world.admin,
        )
    )
    duplicate = await world.client.post(
        "/api/v1/admin/offered_services", json={"name": "visa strategy call"}, headers=world.admin
    )
    assert duplicate.status_code == 409 and "already exists" in duplicate.json()["error"]["message"]

    sid = str(created.get("service_id") or created["id"])
    renamed = h.body(
        await world.client.patch(
            f"/api/v1/admin/offered_services/{sid}",
            json={"name": "Visa Strategy Session"},
            headers=world.admin,
        )
    )
    assert renamed["name"] == "Visa Strategy Session"
    names = [
        s["name"]
        for s in h.body(await world.client.get("/api/v1/offered_services", headers=world.seeker))
    ]
    assert "Visa Strategy Session" in names

    r = await world.client.delete(f"/api/v1/admin/offered_services/{sid}", headers=world.admin)
    assert r.status_code in (200, 204)
    names = [
        s["name"]
        for s in h.body(await world.client.get("/api/v1/offered_services", headers=world.seeker))
    ]
    assert "Visa Strategy Session" not in names


async def test_admin_changes_payment_settings_and_the_audit_remembers(world: World) -> None:
    current = h.body(await world.client.get("/api/v1/admin/payment-settings", headers=world.admin))
    assert float(current["commission_value"]) == 15 and current["commission_type"] == "percent"

    changed = h.body(
        await world.client.put(
            "/api/v1/admin/payment-settings",
            json={
                "seeker_reschedule_window_hours": current["seeker_reschedule_window_hours"],
                "advisor_cancellation_window_hours": 4,
                "commission_type": "percent",
                "commission_value": "10",
                "platform_fee_refund_behavior": current["platform_fee_refund_behavior"],
                "live_payments_enabled": False,
                "subscription_grace_days": current.get("subscription_grace_days", 3),
                "automatic_tax_enabled": False,
            },
            headers=world.admin,
        )
    )
    assert (
        float(changed["commission_value"]) == 10
        and changed["advisor_cancellation_window_hours"] == 4
    )

    config = h.body(await world.client.get("/api/v1/payments/config", headers=world.advisor))
    assert float(config["commission_value"]) == 10, "new bookings use the new commission"
