"""One definition of "what the advisor earned", shared by every screen that shows it.

Before 2026-10-07 four surfaces read the raw ``advisor_payout_usd`` filtered on
``succeeded`` only, so a partially refunded booking vanished from the advisor's
earnings instead of losing the reversed part, and the advisor's own dashboard, both
admin advisor views and the home revenue breakdown disagreed with Consultation Finance
and Financial Analytics (knowledge base section 53).

The rule, identical to the two surfaces that were already right:

    earnings = advisor share of every completed transfer
             - whatever was reversed back off the connected account

``refunded`` rows stay in the row set on purpose: the payout and the reversal cancel to
zero, which is the truth, where excluding the row would either hide it (list/detail) or
go negative (anything that subtracts reversals from a sum that never held the payout).

This module imports models only, so any service can use it without a cycle.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case

from app.core.money import as_float
from app.models.transaction import Transaction, TransactionStatus, TransferStatus

# Every row where money was charged. Abandoned / expired checkouts never count.
CHARGED_STATUSES = (
    TransactionStatus.succeeded,
    TransactionStatus.partially_refunded,
    TransactionStatus.refunded,
)

# SQL expression: gross share minus everything reversed off the advisor's transfer.
# The advisor bears the Stripe fee (QA 2026-10-08): it is reversed off their transfer at
# payment time and booked in ``advisor_reversed_usd`` like any other reversal, so this is
# what the connected account actually kept. A fee that could not be taken (no transfer
# id yet, a Stripe refusal) is therefore not deducted: the advisor still holds it
# (PM exercise, 2026-10-10).
ADVISOR_NET_EARNINGS = Transaction.advisor_payout_usd - Transaction.advisor_reversed_usd

# SQL expression: Stripe's fee once it was actually taken from the advisor. It sits inside
# ``advisor_reversed_usd``; this is the part of the reversals that is not a refund.
STRIPE_FEE_TAKEN = case(
    (Transaction.stripe_fee_reversal_id.is_not(None), Transaction.stripe_fee_usd), else_=0.0
)

# SQL expression: money reversed off the advisor because of refunds (the fee excluded).
REFUND_REVERSALS = Transaction.advisor_reversed_usd - STRIPE_FEE_TAKEN

# SQL expression: platform commission collected minus commission returned, per row.
# Tax is deliberately not in here: it is collected and held for the tax authority and
# is never platform revenue (QA 2026-10-08, section 6).
PLATFORM_NET_FEE = Transaction.commission_usd - Transaction.platform_fee_refunded_usd

# SQL expression: the consultation price, i.e. what the seeker paid less the tax on it.
CONSULTATION_USD = Transaction.amount_usd - Transaction.tax_usd


def advisor_earnings_filters() -> list[Any]:
    """WHERE clauses for an advisor-earnings sum. Combine with the advisor id."""
    return [
        Transaction.is_archived.is_(False),
        Transaction.transfer_status == TransferStatus.completed,
        Transaction.status.in_(CHARGED_STATUSES),
    ]


def charged_filters() -> list[Any]:
    """WHERE clauses for a platform-side sum over every charged row."""
    return [
        Transaction.is_archived.is_(False),
        Transaction.status.in_(CHARGED_STATUSES),
    ]


def advisor_net_earnings(txn: Transaction) -> float:
    """Per-row figure for tables: what the advisor kept of this payment."""
    return round(as_float(txn.advisor_payout_usd) - as_float(txn.advisor_reversed_usd), 2)


def stripe_fee_taken(txn: Transaction) -> float:
    """Per-row twin of ``STRIPE_FEE_TAKEN``."""
    return as_float(txn.stripe_fee_usd) if txn.stripe_fee_reversal_id else 0.0


def refund_reversals(txn: Transaction) -> float:
    """Per-row twin of ``REFUND_REVERSALS``."""
    return round(as_float(txn.advisor_reversed_usd) - stripe_fee_taken(txn), 2)


def consultation_usd(txn: Transaction) -> float:
    """The consultation price: what the seeker paid less the tax on it."""
    return round(as_float(txn.amount_usd) - as_float(txn.tax_usd), 2)
