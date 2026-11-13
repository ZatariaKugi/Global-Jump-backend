"""Phase 2 (#185) · flow 4: plans — free by default, upgrade now, step down at period end."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.flows import helpers as h
from tests.flows.conftest import World, feature

pytestmark = pytest.mark.asyncio


async def _paid_seeker_plan(world: World, *, price: int = 49) -> dict:
    """The admin creates a paid seeker plan (the Stripe product and price come from the fake)."""
    plan = h.body(
        await world.client.post(
            "/api/v1/admin/pricing-plans",
            json={
                "audience": "seeker",
                "name": "Pro",
                "price_usd": str(Decimal(price)),
                "billing_interval": "month",
                "is_highlighted": True,
                "features": [
                    feature("ai_assessments", "number", "unlimited", order=1).model_dump(),
                    feature("assessment_history", "bool", "true", order=2).model_dump(),
                    feature("matched_advisors", "bool", "true", order=3).model_dump(),
                    feature("documents", "bool", "true", order=4).model_dump(),
                    feature("chats", "bool", "true", order=5).model_dump(),
                    feature("visa_journey", "bool", "true", order=6).model_dump(),
                ],
            },
            headers=world.admin,
        )
    )
    assert plan["stripe_price_id"], "a paid plan gets a Stripe price"
    return plan


async def _features(world: World, headers: dict[str, str]) -> dict:
    return h.body(await world.client.get("/api/v1/entitlements/me", headers=headers))


def _enabled(entitlements: dict, key: str) -> bool:
    f = entitlements["features"].get(key)
    if not f:
        return False
    return bool(f.get("enabled", f.get("is_enabled", False)))


async def test_seeker_starts_free_upgrades_then_schedules_a_step_down(world: World) -> None:
    # a new seeker sits on the default free plan: no subscription row, free entitlements
    free = await _features(world, world.seeker)
    assert free["plan_name"].startswith("Free") and not _enabled(free, "visa_journey")
    locked = await world.client.get("/api/v1/users/me/visa-journey", headers=world.seeker)
    assert locked.status_code == 400 and locked.json()["error"]["code"] == "subscription_required"

    pro = await _paid_seeker_plan(world, price=49)

    # upgrade: checkout, then Stripe completes it
    checkout = h.body(
        await world.client.post(
            "/api/v1/subscriptions/checkout", json={"plan_id": pro["id"]}, headers=world.seeker
        )
    )
    assert checkout["checkout_url"].startswith("https://checkout.stripe.test/")
    for event in world.stripe.complete_subscription_checkout(
        checkout["session_id"], amount_cents=4900
    ):
        r = await world.stripe.deliver(world.client, event)
        assert r.status_code == 200, r.text

    me = h.body(await world.client.get("/api/v1/subscriptions/me", headers=world.seeker))
    assert me["plan"]["id"] == pro["id"] and me["status"] == "active"
    paid = await _features(world, world.seeker)
    assert paid["plan_id"] == pro["id"] and _enabled(paid, "visa_journey"), (
        "unlocked by the paid plan"
    )
    assert (
        await world.client.get("/api/v1/users/me/visa-journey", headers=world.seeker)
    ).status_code == 200

    invoices = h.body(
        await world.client.get("/api/v1/subscriptions/me/invoices", headers=world.seeker)
    )
    assert len(invoices) == 1 and float(invoices[0]["amount_usd"]) == 49

    # step down to free: scheduled for the period end, access kept until then
    free_plan = next(
        p
        for p in h.body(await world.client.get("/api/v1/admin/pricing-plans", headers=world.admin))
        if p["audience"] == "seeker" and float(p["price_usd"]) == 0
    )
    scheduled = h.body(
        await world.client.post(
            "/api/v1/subscriptions/me/change-plan",
            json={"plan_id": free_plan["id"]},
            headers=world.seeker,
        )
    )
    assert scheduled["plan"]["id"] == pro["id"], "the paid plan stays until the period ends"
    assert (scheduled.get("scheduled_plan") or {}).get("id") == free_plan["id"] or scheduled[
        "cancel_at_period_end"
    ]
    assert scheduled["access_until"] is not None
    assert world.stripe.called("Refund.create") == [], "subscriptions are never refunded"

    # the platform's books: the payment is credited to the plan it bought
    finance = h.body(
        await world.client.get("/api/v1/admin/subscriptions/summary", headers=world.admin)
    )
    assert float(finance["total_revenue_usd"]) == 49
