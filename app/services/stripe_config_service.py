"""Stripe platform configuration (EPIC 04, PAY-107 / PAY-108).

Two jobs. It reports whether the platform's Stripe setup is present and usable, and
it owns where the credentials come from: the platform settings row, and nowhere else.
There is no environment fallback — an admin must configure Stripe in the panel before
anything can be charged. Nothing here ever returns key material to a caller outside
the service layer; ``key_status`` is what the admin panel sees.

The two secrets are stored as AES-256-GCM ciphertext (``app/core/encryption.py``,
the same helper that protects passport numbers), which is what PAY-108 AC 1 means by
an encrypted config store. The publishable key is public by definition and is stored
as it is so the form can show it back.

The resolved keys are cached for a few seconds and the cache is dropped on save, so a
new key is live on the next request. That matters more than it looks: ``get_settings``
is ``lru_cache``d for the life of the process and uvicorn reloads on ``.py`` only,
which is why a corrected ``.env`` secret went unread for two days in September. A
stored key that needed a restart would be the same bug wearing a form.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import stripe
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import decrypt_field, encrypt_field
from app.core.exceptions import AppError
from app.models.platform_payment_settings import PlatformPaymentSettings, PlatformSettingChange
from app.schemas.payment_settings import StripeKeyStatus, StripeKeysUpdate
from app.services.payment_config_service import PaymentConfig

log = structlog.get_logger()

WEBHOOK_PATH = "/payments/webhook"
KEYS_CACHE_TTL_SECONDS = 5.0

_keys_cache: tuple[StripeKeys, float] | None = None


@dataclass(frozen=True, slots=True)
class StripeKeys:
    """The credentials actually in force, and where they came from."""

    secret_key: str | None
    publishable_key: str | None
    webhook_secret: str | None
    source: Literal["database", "none"]
    # What the admin declared, when they have. ``None`` means nobody has chosen and
    # the mode is only inferable from the key prefix.
    chosen_mode: Literal["test", "live"] | None = None


@dataclass(slots=True)
class StripeStatus:
    mode: str  # "test" | "live" | "unset"
    checks: dict[str, bool] = field(default_factory=dict)
    ok: bool = False


def key_mode(secret_key: str | None) -> str:
    if not secret_key:
        return "unset"
    return "live" if secret_key.startswith(("sk_live", "rk_live")) else "test"


def assert_live_allowed(keys: StripeKeys, config: PaymentConfig) -> None:
    """Live keys need the admin's explicit switch; test keys always work."""
    if not keys.secret_key:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    effective_mode = keys.chosen_mode or key_mode(keys.secret_key)
    if effective_mode == "live" and not config.live_payments_enabled:
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


async def validate(keys: StripeKeys) -> StripeStatus:
    """Presence checks plus two live probes (account, webhook endpoint), never raising."""
    checks: dict[str, bool] = {
        "secret_key_present": bool(keys.secret_key),
        "publishable_key_present": bool(keys.publishable_key),
        "webhook_secret_present": bool(keys.webhook_secret),
        "account_reachable": False,
        # The platform account's own activation, NOT whether Connect is on. Stripe
        # offers no non-destructive probe for Connect: Account.list succeeds on a
        # non-platform account too, and only Account.create(type="express") is
        # decisive. Claiming to measure it is how this row misled everyone.
        "platform_charges_enabled": False,
        "webhook_endpoint_registered": False,
    }
    status = StripeStatus(mode=keys.chosen_mode or key_mode(keys.secret_key), checks=checks)
    if not keys.secret_key:
        return status
    stripe.api_key = keys.secret_key
    try:
        account = await stripe.Account.retrieve_async()
        checks["account_reachable"] = True
        charges = account.get("charges_enabled") if isinstance(account, dict) else None
        if charges is None:
            charges = getattr(account, "charges_enabled", False)
        checks["platform_charges_enabled"] = bool(charges)
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


# ── where the credentials come from (PAY-107 / PAY-108) ──────────────────────


def invalidate_keys_cache() -> None:
    """Drop the resolved keys so the next read sees what was just saved."""
    global _keys_cache
    _keys_cache = None


async def _settings_row(session: AsyncSession) -> PlatformPaymentSettings | None:
    return (await session.execute(select(PlatformPaymentSettings).limit(1))).scalars().first()


def _decrypt(value: str | None, settings: Settings, field_name: str) -> str | None:
    if not value:
        return None
    try:
        return decrypt_field(value, settings)
    except Exception as exc:  # noqa: BLE001 — a stored value we cannot read is not fatal
        # Almost always a changed ENCRYPTION_KEY. The value is unusable either way,
        # so it is treated as unset and the warning names the field.
        log.warning("stripe_key_decrypt_failed", field=field_name, error=type(exc).__name__)
        return None


async def effective_keys(session: AsyncSession, settings: Settings) -> StripeKeys:
    """The Stripe credentials the admin has saved. There is no other source.

    Deliberately no environment fallback: credentials nobody on the admin side can
    see or change are how this project spent two days charging against the wrong
    Stripe account. With nothing stored, payments refuse loudly instead of running
    on whatever the server happens to hold.
    """
    global _keys_cache
    now = time.monotonic()
    if _keys_cache is not None and now - _keys_cache[1] < KEYS_CACHE_TTL_SECONDS:
        return _keys_cache[0]

    row = await _settings_row(session)
    stored_secret = _decrypt(getattr(row, "stripe_secret_key_enc", None), settings, "secret_key")
    stored_webhook = _decrypt(
        getattr(row, "stripe_webhook_secret_enc", None), settings, "webhook_secret"
    )
    stored_publishable = getattr(row, "stripe_publishable_key", None) or None

    source: Literal["database", "none"] = (
        "database" if (stored_secret or stored_publishable or stored_webhook) else "none"
    )

    stored_mode = getattr(row, "stripe_mode", None)
    chosen: Literal["test", "live"] | None = None
    if stored_mode == "live":
        chosen = "live"
    elif stored_mode == "test":
        chosen = "test"

    keys = StripeKeys(
        secret_key=stored_secret,
        publishable_key=stored_publishable,
        webhook_secret=stored_webhook,
        source=source,
        chosen_mode=chosen,
    )
    _keys_cache = (keys, now)
    return keys


def _last4(value: str | None) -> str | None:
    return value[-4:] if value and len(value) >= 4 else None


def _mode_literal(secret_key: str | None) -> Literal["test", "live", "unknown"]:
    mode = key_mode(secret_key)
    if mode == "live":
        return "live"
    return "test" if mode == "test" else "unknown"


async def key_status(session: AsyncSession, settings: Settings) -> StripeKeyStatus:
    """What the admin form may display: presence, last four, mode, origin."""
    keys = await effective_keys(session, settings)
    return StripeKeyStatus(
        source=keys.source,
        # What the admin chose wins over what the key looks like: the choice is the
        # thing the keys are checked against, so it is also the thing to report.
        mode=keys.chosen_mode or _mode_literal(keys.secret_key),
        mode_is_chosen=keys.chosen_mode is not None,
        secret_key_set=bool(keys.secret_key),
        secret_key_last4=_last4(keys.secret_key),
        publishable_key=keys.publishable_key,
        webhook_secret_set=bool(keys.webhook_secret),
        webhook_secret_last4=_last4(keys.webhook_secret),
    )


# ── validation before saving ─────────────────────────────────────────────────


async def _assert_secret_key_works(secret_key: str) -> None:
    """Ask Stripe who this key belongs to. The only check that proves anything."""
    if not secret_key.startswith(("sk_", "rk_")):
        raise AppError(
            "That does not look like a Stripe secret key (it should start with sk_).",
            code="stripe_secret_key_invalid",
        )
    previous = stripe.api_key
    stripe.api_key = secret_key
    try:
        await stripe.Account.retrieve_async()
    except stripe.StripeError as exc:
        log.warning("stripe_secret_key_rejected", error=str(exc)[:200])
        raise AppError(
            "Stripe rejected that secret key. Check it and try again.",
            code="stripe_secret_key_invalid",
        ) from exc
    finally:
        stripe.api_key = previous


def _any_key_mode(key: str | None) -> str:
    """live/test for any Stripe key, publishable or secret.

    ``key_mode`` only recognises secret-key prefixes, so it answers "test" for a
    ``pk_live_`` key — which is precisely the mismatch that has to be caught here.
    """
    if not key:
        return "unset"
    return "live" if key.startswith(("sk_live", "rk_live", "pk_live")) else "test"


def _assert_matches_mode(mode: str, key: str | None, label: str) -> None:
    """A key must belong to the environment the admin said this platform is on.

    Without this the mode would be decorative: whatever was pasted would decide, and
    a live key under a "Test" label would go live silently on the next payment.
    """
    if key and _any_key_mode(key) != mode:
        raise AppError(
            f"That {label} is for Stripe's {_any_key_mode(key)} environment, "
            f"but this platform is set to {mode} mode.",
            code="stripe_key_mode_mismatch",
        )


def _assert_publishable_key(publishable_key: str, secret_key: str | None) -> None:
    if not publishable_key.startswith("pk_"):
        raise AppError(
            "That does not look like a Stripe publishable key (it should start with pk_).",
            code="stripe_publishable_key_invalid",
        )
    # The browser and the server must point at the same Stripe mode, or payments fail
    # in front of a customer rather than here.
    if secret_key and _any_key_mode(publishable_key) != _any_key_mode(secret_key):
        raise AppError(
            "The publishable key is for a different Stripe mode than the secret key.",
            code="stripe_key_mode_mismatch",
        )


def _assert_webhook_secret(webhook_secret: str) -> None:
    """The strictest check available for the one value that fails silently.

    Stripe never hands a signing secret back, so it cannot be verified against the
    API. A wrong one answers 400 before the ledger write, leaving no row and no clue,
    so the shape is checked here rather than discovered days later.
    """
    if not webhook_secret.startswith("whsec_") or len(webhook_secret) < 16:
        raise AppError(
            "That does not look like a Stripe webhook signing secret "
            "(it should start with whsec_).",
            code="stripe_webhook_secret_invalid",
        )


def _record(
    session: AsyncSession,
    settings_id: uuid.UUID,
    key: str,
    changed: bool,
    admin_id: uuid.UUID | None,
) -> None:
    """Audit that a credential changed, never what it changed to (PAY-107 AC 4)."""
    if not changed:
        return
    session.add(
        PlatformSettingChange(
            settings_id=settings_id,
            setting_key=key,
            old_value="(hidden)",
            new_value="(updated)",
            changed_by=admin_id,
        )
    )


async def save_keys(
    session: AsyncSession,
    data: StripeKeysUpdate,
    admin_id: uuid.UUID | None,
    settings: Settings,
) -> StripeKeyStatus:
    """Validate, then store. Nothing is written unless every supplied value passes.

    A partially applied save is the worst outcome available here — a new secret key
    beside the old webhook secret, say — so all three are checked before any is
    written.
    """
    row = await _settings_row(session)
    if row is None:
        row = PlatformPaymentSettings()
        session.add(row)
        await session.flush()

    current = await effective_keys(session, settings)
    secret_for_mode = data.secret_key or current.secret_key
    mode = data.mode or current.chosen_mode

    # The declared mode is checked first: a live key arriving under "Test" is the one
    # mistake on this form that spends real money, and it must not reach Stripe at all.
    if mode:
        _assert_matches_mode(mode, data.secret_key, "secret key")
        _assert_matches_mode(mode, data.publishable_key, "publishable key")
    if data.secret_key:
        await _assert_secret_key_works(data.secret_key)
    if data.publishable_key:
        _assert_publishable_key(data.publishable_key, secret_for_mode)
    if data.webhook_secret:
        _assert_webhook_secret(data.webhook_secret)

    if data.mode:
        row.stripe_mode = data.mode
    if data.secret_key:
        row.stripe_secret_key_enc = encrypt_field(data.secret_key, settings)
    if data.webhook_secret:
        row.stripe_webhook_secret_enc = encrypt_field(data.webhook_secret, settings)
    if data.publishable_key:
        row.stripe_publishable_key = data.publishable_key

    _record(session, row.id, "stripe_mode", bool(data.mode), admin_id)
    _record(session, row.id, "stripe_secret_key", bool(data.secret_key), admin_id)
    _record(session, row.id, "stripe_webhook_secret", bool(data.webhook_secret), admin_id)
    _record(session, row.id, "stripe_publishable_key", bool(data.publishable_key), admin_id)

    session.add(row)
    await session.flush()
    invalidate_keys_cache()
    log.info(
        "stripe_keys_saved",
        admin_id=str(admin_id),
        secret_key=bool(data.secret_key),
        publishable_key=bool(data.publishable_key),
        webhook_secret=bool(data.webhook_secret),
    )
    return await key_status(session, settings)
