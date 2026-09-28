"""Stripe platform configuration checks (EPIC 04, PAY-108).

Keys stay in the environment / secrets manager. This module only reports whether
they are present and usable, and gates live processing on the admin switch. No
function here ever returns key material.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import stripe
import structlog

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.payment_config_service import PaymentConfig

log = structlog.get_logger()

WEBHOOK_PATH = "/payments/webhook"


@dataclass(slots=True)
class StripeStatus:
    mode: str  # "test" | "live" | "unset"
    checks: dict[str, bool] = field(default_factory=dict)
    ok: bool = False


def key_mode(secret_key: str | None) -> str:
    if not secret_key:
        return "unset"
    return "live" if secret_key.startswith(("sk_live", "rk_live")) else "test"


def assert_live_allowed(settings: Settings, config: PaymentConfig) -> None:
    """Live keys need the admin's explicit switch; test keys always work."""
    if not settings.STRIPE_SECRET_KEY:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    if key_mode(settings.STRIPE_SECRET_KEY) == "live" and not config.live_payments_enabled:
        raise AppError(
            "Live payments are switched off in the admin settings", code="payments_disabled"
        )


def checkout_failed(exc: Exception, **context: str) -> AppError:
    """Turn a Stripe refusal during checkout into a coded error the client can use.

    Left uncaught, a ``StripeError`` escapes the route as a 500, and the frontend copy
    table — which keys off the error code — has nothing to translate, so the user sees
    a blank failure. The Stripe message goes to the log, not to the client: it names
    prices and accounts that mean nothing to a seeker.
    """
    log.warning("checkout_stripe_error", error=str(exc)[:300], **context)
    return AppError("Checkout could not be started", code="checkout_failed")


async def validate(settings: Settings) -> StripeStatus:
    """Presence checks plus two live probes (account, webhook endpoint), never raising."""
    checks: dict[str, bool] = {
        "secret_key_present": bool(settings.STRIPE_SECRET_KEY),
        "publishable_key_present": bool(settings.STRIPE_PUBLISHABLE_KEY),
        "webhook_secret_present": bool(settings.STRIPE_WEBHOOK_SECRET),
        "account_reachable": False,
        "connect_enabled": False,
        "webhook_endpoint_registered": False,
    }
    status = StripeStatus(mode=key_mode(settings.STRIPE_SECRET_KEY), checks=checks)
    if not settings.STRIPE_SECRET_KEY:
        return status
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        account = await stripe.Account.retrieve_async()
        checks["account_reachable"] = True
        charges = account.get("charges_enabled") if isinstance(account, dict) else None
        if charges is None:
            charges = getattr(account, "charges_enabled", False)
        checks["connect_enabled"] = bool(charges)
    except Exception as exc:  # noqa: BLE001 — a status probe must never 500
        log.warning("stripe_account_probe_failed", error=type(exc).__name__)
    try:
        endpoints = await stripe.WebhookEndpoint.list_async(limit=100)
        data = (
            endpoints.get("data") if isinstance(endpoints, dict) else getattr(endpoints, "data", [])
        )
        for ep in data or []:
            url = ep.get("url") if isinstance(ep, dict) else getattr(ep, "url", "")
            st = ep.get("status") if isinstance(ep, dict) else getattr(ep, "status", "")
            if str(url or "").endswith(WEBHOOK_PATH) and str(st or "enabled") == "enabled":
                checks["webhook_endpoint_registered"] = True
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("stripe_webhook_probe_failed", error=type(exc).__name__)
    status.ok = all(checks.values())
    return status
