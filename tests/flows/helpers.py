"""User-story helpers shared by the flow tests: each one is a step a person takes."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

from tests.flows.conftest import PASSWORD, World


def body(resp: Any) -> Any:
    assert resp.status_code in (200, 201), f"{resp.status_code} {resp.text}"
    return resp.json()["data"]


# ---------------------------------------------------------------- registration and sign-in
async def register_seeker(
    world: World, email: str, full_name: str = "New Seeker"
) -> dict[str, Any]:
    return body(
        await world.client.post(
            "/api/v1/auth/register",
            json={"email": email, "full_name": full_name, "password": PASSWORD},
        )
    )


async def register_advisor(
    world: World, email: str, full_name: str = "New Advisor"
) -> dict[str, Any]:
    return body(
        await world.client.post(
            "/api/v1/auth/register/advisor",
            json={"email": email, "full_name": full_name, "password": PASSWORD},
        )
    )


async def verify_email(world: World, email: str) -> dict[str, Any]:
    """Click the link from the verification e-mail (the token the auth route scheduled)."""
    token = world.last_mail("verify", email).token
    return body(await world.client.post("/api/v1/auth/verify-email", json={"token": token}))


async def login(world: World, email: str) -> dict[str, str]:
    return await world.login(email)


# ---------------------------------------------------------------- admin
async def approve_advisor(world: World, advisor_id: str | uuid.UUID) -> dict[str, Any]:
    return body(
        await world.client.patch(
            f"/api/v1/admin/advisors/{advisor_id}/verification",
            json={"status": "approved"},
            headers=world.admin,
        )
    )


# ---------------------------------------------------------------- advisor readiness
async def connect_stripe(world: World, headers: dict[str, str]) -> str:
    """Start Connect onboarding, then finish it the way Stripe does: ``account.updated``."""
    status = body(await world.client.post("/api/v1/advisors/me/stripe-connect", headers=headers))
    account_id = (
        str(status.get("stripe_account_id") or status.get("account_id") or "")
        or list(world.stripe.accounts)[-1]
    )
    event = world.stripe.enable_account(account_id)
    r = await world.stripe.deliver(world.client, event)
    assert r.status_code == 200, r.text
    return account_id


async def connect_zoom(world: World, headers: dict[str, str]) -> str:
    """Start the OAuth dance, then come back from Zoom with a code (exchange is faked)."""
    start = body(
        await world.client.post("/api/v1/advisors/me/integrations/zoom/connect", headers=headers)
    )
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    r = await world.client.get(
        "/api/v1/integrations/zoom/callback",
        params={"code": "flows-code", "state": state},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 307), r.text
    return r.headers["location"]


async def find_advisor_ids(world: World, headers: dict[str, str], **params: Any) -> list[str]:
    rows = body(
        await world.client.get(
            "/api/v1/advisors", params={"page_size": 100, **params}, headers=headers
        )
    )
    return [str(r.get("user_id") or r.get("id")) for r in rows]
