"""Consultation tax via Stripe Tax (QA 2026-10-08, "Tax Execution"; PM ruling the same day).

Nothing here decides a rate. When the admin switch ``automatic_tax_enabled`` is on, the
Checkout Session is created with ``automatic_tax.enabled`` and the consultation price as
a tax-*exclusive* line, and **Stripe** works out the tax from the seeker's billing
address, the platform's tax registrations and the product tax code configured in the
Stripe Dashboard (Settings → Tax). The seeker sees Consultation + Tax = Total on
Stripe's page; this module only reads back what Stripe charged and snapshots it on the
transaction so every later screen agrees with Stripe.

Rules the QA document fixes and the rest of the service enforces:

- Tax is added on top of the price and held by the platform: it is never revenue, never
  the advisor's, and never refunded (``refund_engine.refundable_total``).
- The backend is authoritative: the client previews only "tax will be added"; the
  amount comes from Stripe at ``checkout.session.completed``.
- Stripe Tax must be activated on the platform Stripe account first (origin address,
  registrations, preset product tax code). With the switch on and Stripe Tax inactive,
  Stripe refuses to create the session and checkout reports ``checkout_failed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import stripe
import structlog

log = structlog.get_logger()

_CENT = Decimal("0.01")

# Expanding the breakdown on a Checkout Session retrieve yields the rate objects.
BREAKDOWN_EXPAND = ["total_details.breakdown"]


@dataclass(frozen=True, slots=True)
class TaxSnapshot:
    """What Stripe charged as tax on one Checkout Session, in dollars."""

    amount_usd: Decimal
    total_usd: Decimal | None  # what the seeker paid in total, when the session says so
    rate_percent: Decimal  # 0 when Stripe gave no rate
    label: str | None  # Stripe's display name: "VAT", "Sales Tax", "GST"
    country: str | None
    jurisdiction: str | None  # "California", "Germany", ...
    has_breakdown: bool = False  # the per-rate breakdown was on the payload

    @property
    def applies(self) -> bool:
        return self.amount_usd > 0

    @property
    def rate(self) -> Decimal:
        """Fraction for ``transactions.tax_rate`` (0.1000 for 10%)."""
        return (self.rate_percent / Decimal(100)).quantize(Decimal("0.0001"))


NO_TAX = TaxSnapshot(
    amount_usd=Decimal("0.00"),
    total_usd=None,
    rate_percent=Decimal("0"),
    label=None,
    country=None,
    jurisdiction=None,
)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a StripeObject or plain dict (webhook payload either way)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(obj, key, default)


def _cents_to_usd(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return (Decimal(int(value)) / Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP)


def snapshot_from_session(cs: Any) -> TaxSnapshot:
    """Pure read of a completed Checkout Session.

    ``total_details.amount_tax`` is always on the session object (the webhook payload
    included); the per-rate breakdown is only there when it was expanded, so the label,
    rate and jurisdiction are best effort and ``amount`` alone still records the tax.
    """
    details = _get(cs, "total_details")
    amount = _cents_to_usd(_get(details, "amount_tax"))
    if amount is None or amount <= 0:
        return NO_TAX
    total = _cents_to_usd(_get(cs, "amount_total"))

    rate_percent = Decimal("0")
    label = country = jurisdiction = None
    has_breakdown = False
    taxes = _get(_get(details, "breakdown"), "taxes") or []
    first = taxes[0] if isinstance(taxes, (list, tuple)) and taxes else None
    rate_obj = _get(first, "rate")
    if rate_obj is not None and not isinstance(rate_obj, str):
        has_breakdown = True
        pct = _get(rate_obj, "percentage")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            rate_percent = Decimal(str(pct)).quantize(_CENT)
        label = (_get(rate_obj, "display_name") or None) and str(_get(rate_obj, "display_name"))
        country = (_get(rate_obj, "country") or None) and str(_get(rate_obj, "country")).upper()
        state = _get(rate_obj, "state")
        juris = _get(rate_obj, "jurisdiction")
        jurisdiction = str(juris) if juris else (str(state) if state else None)
    if rate_percent == 0 and total is not None and total > amount:
        # No rate object: derive the effective percentage from the amounts.
        subtotal = _cents_to_usd(_get(cs, "amount_subtotal")) or (total - amount)
        if subtotal > 0:
            rate_percent = (amount / subtotal * Decimal(100)).quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
    return TaxSnapshot(
        amount_usd=amount,
        total_usd=total,
        rate_percent=rate_percent,
        label=label or "Tax",
        country=country[:2] if country else None,
        jurisdiction=jurisdiction[:100] if jurisdiction else None,
        has_breakdown=has_breakdown,
    )


async def snapshot_for_session(cs: Any, session_id: str) -> TaxSnapshot:
    """The snapshot, with the rate breakdown fetched from Stripe when the webhook payload
    carried tax but no breakdown. Any Stripe error keeps the amounts already known."""
    first = snapshot_from_session(cs)
    if not first.applies:
        return first
    if first.has_breakdown:
        return first  # the payload already carried the rate, label and jurisdiction
    try:
        full = await stripe.checkout.Session.retrieve_async(session_id, expand=BREAKDOWN_EXPAND)
    except stripe.StripeError as exc:
        log.warning("tax_breakdown_retrieve_failed", session_id=session_id, error=str(exc))
        return first
    second = snapshot_from_session(full)
    return second if second.applies else first


def config_read(automatic_tax_enabled: bool) -> dict[str, object]:
    """The one fact the client needs for the pay-sheet preview."""
    return {"automatic_tax_enabled": bool(automatic_tax_enabled)}
