"""Phase 1 (#184): the test world and its fakes, proven by the first two user stories.

1. A seeker registers, verifies the e-mail from the captured mail, logs in.
2. An advisor registers, fills the profile while pending, is approved by an admin, connects
   Stripe (fake account + signed ``account.updated``) and Zoom (fake OAuth), and only then
   becomes visible on Find Advisor — the readiness gate seekers rely on.
Plus one proving test per fake.
"""

from __future__ import annotations

import json

import pytest

from tests.flows import helpers as h
from tests.flows.conftest import PASSWORD, World

pytestmark = pytest.mark.asyncio


async def test_seeker_registers_verifies_and_logs_in(world: World) -> None:
    created = await h.register_seeker(world, "new-seeker@flows-world.com")
    assert created["email"] == "new-seeker@flows-world.com"
    assert world.last_mail("verify", "new-seeker@flows-world.com").token

    verified = await h.verify_email(world, "new-seeker@flows-world.com")
    assert verified["email"] == "new-seeker@flows-world.com"

    headers = await h.login(world, "new-seeker@flows-world.com")
    me = h.body(await world.client.get("/api/v1/users/me", headers=headers))
    assert me["role"] == "seeker"


async def test_advisor_is_approved_connects_stripe_and_zoom_and_appears_on_find_advisor(
    world: World,
) -> None:
    email = "new-advisor@flows-world.com"
    created = await h.register_advisor(world, email, "Flows New Advisor")
    await h.verify_email(world, email)

    # a pending advisor can sign in (to finish onboarding and see the pending screen) …
    pending = await world.client.post(
        "/api/v1/auth/login", data={"username": email, "password": PASSWORD}
    )
    assert pending.status_code == 200 and pending.json()["verification_status"] == "pending"
    pending_headers = {"Authorization": f"Bearer {pending.json()['access_token']}"}
    # … and fill in the minimum profile a seeker can match on
    h.body(
        await world.client.patch(
            "/api/v1/advisors/me/profile",
            json={
                "title": "AU Student Visa Advisor",
                "years_of_experience": 6,
                "country_of_residence": "PK",
                "country_expertise": ["AU"],
                "visa_specializations": ["student"],
            },
            headers=pending_headers,
        )
    )
    assert created["id"] not in await h.find_advisor_ids(world, world.seeker), (
        "a pending advisor must not be listed"
    )

    await h.approve_advisor(world, created["id"])
    headers = await h.login(world, email)
    assert created["id"] not in await h.find_advisor_ids(world, world.seeker), (
        "an approved advisor without Stripe and Zoom must not be listed"
    )

    account_id = await h.connect_stripe(world, headers)
    assert world.stripe.accounts[account_id]["payouts_enabled"] is True
    status = h.body(await world.client.get("/api/v1/advisors/me/stripe-connect", headers=headers))
    assert status["payouts_enabled"] is True and status["stripe_account_id"] == account_id

    location = await h.connect_zoom(world, headers)
    assert "zoom=connected" in location
    assert world.zoom.called("exchange_code") and world.zoom.called("fetch_zoom_user")

    after = await h.find_advisor_ids(world, world.seeker)
    assert created["id"] in after


# ----------------------------------------------------------------------------- the fakes
async def test_stripe_fake_rejects_a_bad_signature_and_accepts_a_good_one(world: World) -> None:
    event = world.stripe.event(
        "account.updated", {"id": "acct_unknown", "object": "account", "payouts_enabled": True}
    )
    payload = json.dumps(event).encode()
    bad = await world.client.post(
        "/api/v1/payments/webhook",
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_signature"
    good = await world.stripe.deliver(world.client, event)
    assert good.status_code == 200


async def test_stripe_fake_checkout_pays_like_stripe(world: World) -> None:
    import stripe

    session = await stripe.checkout.Session.create_async(
        mode="payment",
        line_items=[{"price_data": {"unit_amount": 10000, "currency": "usd"}, "quantity": 1}],
        metadata={"booking_id": "b1"},
        payment_intent_data={
            "application_fee_amount": 1000,
            "transfer_data": {"destination": "acct_adv"},
        },
    )
    assert (
        session["url"].startswith("https://checkout.stripe.test/")
        and session["payment_status"] == "unpaid"
    )

    event = world.stripe.complete_checkout(session["id"], fee_cents=320)
    assert (
        event["type"] == "checkout.session.completed"
        and event["data"]["object"]["payment_status"] == "paid"
    )

    pi = await stripe.PaymentIntent.retrieve_async(
        event["data"]["object"]["payment_intent"],
        expand=["latest_charge", "latest_charge.balance_transaction"],
    )
    charge = pi["latest_charge"]
    assert (
        charge["balance_transaction"]["fee"] == 320 and charge["transfer"] in world.stripe.transfers
    )
    assert world.stripe.transfers[charge["transfer"]]["amount"] == 9000

    refund = await stripe.Refund.create_async(payment_intent=pi["id"], amount=2500)
    assert (
        refund["status"] == "succeeded"
        and world.stripe.charges[charge["id"]]["amount_refunded"] == 2500
    )


async def test_ai_fake_answers_in_the_shapes_the_services_validate(world: World) -> None:
    from app.services import ai_advisor_match_service, ai_insight_service

    a, b = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    rerank = world.ai.complete(
        {
            "messages": [
                {"role": "system", "content": ai_advisor_match_service._SYSTEM_PROMPT},
                {"role": "user", "content": f"advisors: {a}\n{b}"},
            ]
        }
    )
    parsed = ai_advisor_match_service._RawRerank.model_validate(
        json.loads(rerank.choices[0].message.content)
    )
    assert [m.advisor_id for m in parsed.matches] == [a, b] and parsed.matches[
        0
    ].ai_score > parsed.matches[1].ai_score

    insights = world.ai.complete(
        {
            "messages": [
                {"role": "system", "content": ai_insight_service._SYSTEM_PROMPT},
                {"role": "user", "content": "profile"},
            ]
        }
    )
    ai_insight_service._RawInsights.model_validate(json.loads(insights.choices[0].message.content))


async def test_push_fake_records_sends(world: World) -> None:
    from firebase_admin import messaging

    batch = messaging.send_each_for_multicast(
        messaging.MulticastMessage(
            tokens=["t1", "t2"], notification=messaging.Notification(title="hi", body="there")
        )
    )
    assert batch.success_count == 2 and len(world.push.sent) == 1
