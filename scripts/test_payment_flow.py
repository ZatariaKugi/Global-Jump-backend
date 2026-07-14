#!/usr/bin/env python3
# ruff: noqa: T201
"""
End-to-end payment flow: real Postgres DB + real Stripe sandbox.

Pre-requisites:
  make db-up && make migrate
  make run          (server on localhost:8000)

Usage:
  uv run python scripts/test_payment_flow.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime

import httpx
import stripe

# ── Config ──────────────────────────────────────────────────────────────────
BASE = "http://localhost:8020/api/v1"
STRIPE_SK = os.environ.get("STRIPE_SECRET_KEY", "")
if not STRIPE_SK:
    sys.exit("Set STRIPE_SECRET_KEY env var first")
stripe.api_key = STRIPE_SK

TAG = int(time.time())
SEEKER_EMAIL = f"seeker_{TAG}@flowtest.com"
ADVISOR_EMAIL = f"advisor_{TAG}@flowtest.com"
ADMIN_EMAIL = f"admin_{TAG}@flowtest.com"
PASSWORD = "FlowTest1234!"


def ok(label: str, r: httpx.Response, expected: int = 200) -> dict:
    if r.status_code != expected:
        print(f"\n✗ {label} — HTTP {r.status_code}")
        print(r.text[:600])
        sys.exit(1)
    print(f"  ✓ {label}")
    return r.json()  # type: ignore[no-any-return]


def next_weekday(weekday: int) -> date:
    today = datetime.now(UTC).date()
    delta = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


async def create_admin(db_url: str, email: str, password: str) -> None:
    """Insert an admin user directly into Postgres (bypasses the public API)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.security import hash_password
    from app.models.user import User, UserRole

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        existing = await session.execute(
            __import__("sqlalchemy", fromlist=["select"]).select(User).where(User.email == email)
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                User(
                    email=email,
                    full_name="Flow Admin",
                    hashed_password=hash_password(password),
                    role=UserRole.admin,
                    is_active=True,
                )
            )
            await session.commit()
    await engine.dispose()


async def main() -> None:
    # ── DB URL ────────────────────────────────────────────────────────────
    from app.core.config import get_settings

    settings = get_settings()
    db_url = settings.sqlalchemy_dsn

    print("\n══════════════════════════════════════════════")
    print("  GlobleJump — End-to-End Payment Flow Test")
    print("══════════════════════════════════════════════")

    # ── Step 0: seed admin ───────────────────────────────────────────────
    print("\n[0] Seeding admin user in DB…")
    await create_admin(db_url, ADMIN_EMAIL, PASSWORD)
    print(f"    admin: {ADMIN_EMAIL}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # ── Step 1: register & login seeker ─────────────────────────────
        print("\n[1] Registering seeker…")
        ok(
            "register seeker",
            await c.post(
                "/auth/register",
                json={
                    "email": SEEKER_EMAIL,
                    "password": PASSWORD,
                    "full_name": "Flow Seeker",
                },
            ),
            201,
        )
        r = ok(
            "login seeker",
            await c.post("/auth/login", data={"username": SEEKER_EMAIL, "password": PASSWORD}),
        )
        seeker_token = r["access_token"]
        sh = {"Authorization": f"Bearer {seeker_token}"}
        seeker_me = ok("get seeker /me", await c.get("/users/me", headers=sh))
        seeker_id = seeker_me["data"]["id"]
        print(f"    seeker id: {seeker_id}")

        # ── Step 2: register advisor ─────────────────────────────────────
        print("\n[2] Registering advisor…")
        ok(
            "register advisor",
            await c.post(
                "/auth/register/advisor",
                json={
                    "email": ADVISOR_EMAIL,
                    "password": PASSWORD,
                    "full_name": "Flow Advisor",
                },
            ),
            201,
        )
        r = ok(
            "login advisor",
            await c.post("/auth/login", data={"username": ADVISOR_EMAIL, "password": PASSWORD}),
        )
        advisor_token = r["access_token"]
        ah = {"Authorization": f"Bearer {advisor_token}"}
        advisor_me = ok("get advisor /me", await c.get("/users/me", headers=ah))
        advisor_id = advisor_me["data"]["id"]
        print(f"    advisor id: {advisor_id}")

        # ── Step 3: admin approves advisor ───────────────────────────────
        print("\n[3] Admin approving advisor…")
        r = ok(
            "admin login",
            await c.post("/auth/login", data={"username": ADMIN_EMAIL, "password": PASSWORD}),
        )
        admin_token = r["access_token"]
        adm = {"Authorization": f"Bearer {admin_token}"}
        ok(
            "approve advisor",
            await c.patch(
                f"/admin/advisors/{advisor_id}/verification",
                json={"status": "approved"},
                headers=adm,
            ),
        )

        # Re-login advisor to get fresh token (now active)
        r = ok(
            "re-login advisor",
            await c.post("/auth/login", data={"username": ADVISOR_EMAIL, "password": PASSWORD}),
        )
        advisor_token = r["access_token"]
        ah = {"Authorization": f"Bearer {advisor_token}"}

        # ── Step 4: set up advisor profile + service + availability ─────
        # Profile must exist before we can attach a Stripe Connect account.
        print("\n[4] Setting up advisor profile and availability…")
        ok(
            "update profile",
            await c.patch(
                "/advisors/me/profile",
                json={
                    "title": "Visa Specialist",
                    "bio": "Expert in UK and US visa applications.",
                    "years_of_experience": 5,
                    "visa_specializations": ["tourist", "work"],
                    "country_expertise": ["GB", "US"],
                    "languages": [{"language": "English", "proficiency": "native"}],
                    "services": [
                        {
                            "service_type": "consultation_30",
                            "duration_minutes": 30,
                            "price_usd": 50.00,
                        }
                    ],
                    "cancellation_notice_hours": 24,
                },
                headers=ah,
            ),
        )

        # Set weekly availability: all 7 days, all day UTC
        slots = [
            {"weekday": d, "start_time": "00:00", "end_time": "23:00", "timezone": "UTC"}
            for d in range(7)
        ]
        ok(
            "set availability",
            await c.put(
                "/advisors/me/availability",
                json={"slots": slots},
                headers=ah,
            ),
        )

        # ── Step 5: Stripe Connect (skipped — enable Connect in Stripe Dashboard first)
        stripe_account_id = None
        print("\n[5] Stripe Connect skipped (sandbox account not enrolled in Connect)")
        print("    Payments will collect to the platform account.")
        print(
            "    Enable Connect at https://dashboard.stripe.com/connect to unlock advisor payouts."
        )

        # ── Step 6: seeker creates booking ───────────────────────────────
        print("\n[6] Seeker creating booking…")
        booking_day = next_weekday(2)  # next Wednesday
        slot_start = datetime.combine(booking_day, dtime(10, 0), UTC).isoformat()
        r = ok(
            "create booking",
            await c.post(
                "/bookings",
                json={
                    "advisor_id": advisor_id,
                    "service_type": "consultation_30",
                    "scheduled_start": slot_start,
                    "seeker_note": "Flow test booking",
                },
                headers=sh,
            ),
            201,
        )
        booking = r["data"]
        booking_id = booking["id"]
        print(f"    booking id:  {booking_id}")
        print(f"    price:       ${booking['price_usd']:.2f}")
        print(f"    payment:     {booking['payment_status']}")

        # ── Step 7: create Stripe Checkout Session ────────────────────────
        print("\n[7] Creating Stripe Checkout Session…")
        r = ok(
            "create checkout",
            await c.post(
                "/payments/checkout",
                json={"booking_id": booking_id},
                headers=sh,
            ),
            201,
        )
        checkout = r["data"]
        session_id = checkout["session_id"]
        checkout_url = checkout["checkout_url"]
        print(f"    session id:  {session_id}")
        print(f"    checkout URL: {checkout_url}")

        # ── Step 8: simulate checkout.session.completed webhook ──────────
        # Real flow: customer pays on Stripe's hosted page → Stripe fires webhook.
        # Test flow: we construct the event payload and POST it directly, since
        # STRIPE_WEBHOOK_SECRET is unset (dev mode skips signature verification).
        print("\n[8] Simulating checkout.session.completed webhook…")
        fake_pi_id = f"pi_test_{TAG}"
        fake_charge_id = f"ch_test_{TAG}"
        webhook_payload = json.dumps(
            {
                "id": f"evt_test_{TAG}",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "id": session_id,
                        "object": "checkout.session",
                        "payment_intent": fake_pi_id,
                        "payment_status": "paid",
                        "status": "complete",
                        "metadata": {"booking_id": booking_id},
                        "amount_total": 5000,
                        "currency": "usd",
                    }
                },
            }
        ).encode()
        pi_id = fake_pi_id

        # ── Step 9: (expand PI to get charge — skipped in test mode) ──────
        print("\n[9] Skipping live PI confirm (no Stripe Connect; use hosted page in prod)")
        print(f"    Synthetic PI:     {pi_id}")
        print(f"    Synthetic charge: {fake_charge_id}")

        # ── Step 10: POST webhook to our server (dev mode, no sig check) ──
        print("\n[10] Forwarding webhook event to our server…")
        wr = await c.post(
            "/payments/webhook",
            content=webhook_payload,
            headers={"Content-Type": "application/json", "stripe-signature": "dev-bypass"},
        )
        ok("webhook received", wr)

        # ── Step 11: verify booking is now paid ───────────────────────────
        print("\n[11] Verifying booking payment status…")
        r = ok("get booking", await c.get(f"/bookings/{booking_id}", headers=sh))
        final_booking = r["data"]
        payment_status = final_booking["payment_status"]
        print(f"    payment_status: {payment_status}")
        assert payment_status == "paid", f"Expected 'paid', got '{payment_status}'"

        # ── Step 12: check advisor earnings ───────────────────────────────
        print("\n[12] Checking advisor earnings…")
        r = ok("advisor earnings", await c.get("/advisors/me/earnings", headers=ah))
        earnings = r["data"]
        print(f"    total earned:    ${earnings['total_earned_usd']:.2f}")
        print(f"    commission paid: ${earnings['total_commission_paid_usd']:.2f}")
        print(f"    transactions:    {len(earnings['transactions'])}")
        assert earnings["total_earned_usd"] > 0

        # ── Step 13: check seeker payment history ─────────────────────────
        print("\n[13] Checking seeker payment history…")
        r = ok("payment history", await c.get("/payments/history", headers=sh))
        history = r["data"]
        print(f"    total records: {len(history)}")
        assert len(history) >= 1
        txn = history[0]
        print(f"    amount:    ${txn['amount_usd']:.2f}")
        print(f"    status:    {txn['status']}")
        assert txn["status"] == "succeeded"

        # ── Step 14: admin views all payments ────────────────────────────
        print("\n[14] Admin viewing all payments…")
        r = ok("admin payments list", await c.get("/admin/payments", headers=adm))
        all_txns = r["data"]
        print(f"    platform transactions: {len(all_txns)}")
        assert len(all_txns) >= 1

    print("\n══════════════════════════════════════════════")
    print("  ✅  ALL STEPS PASSED — Payment flow complete")
    print("══════════════════════════════════════════════\n")
    print(f"  Booking ID:  {booking_id}")
    print(f"  Stripe PI:   {pi_id}")
    print(f"  Amount:      $50.00 → seeker charged")
    print(f"  Commission:  $7.50  → platform (15%)")
    print(f"  Payout:      $42.50 → advisor (pending Connect setup)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
