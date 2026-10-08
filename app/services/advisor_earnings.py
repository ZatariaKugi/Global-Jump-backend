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

from app.core.money import as_float
from app.models.transaction import Transaction, TransactionStatus, TransferStatus

# Every row where money was charged. Abandoned / expired checkouts never count.
CHARGED_STATUSES = (
    TransactionStatus.succeeded,
    TransactionStatus.partially_refunded,
    TransactionStatus.refunded,
)

# SQL expression: gross share minus Stripe's processing fee minus reversal, per row.
# The advisor bears the Stripe fee (QA 2026-10-08): it is reversed off their transfer at
# payment time, so their net is what the connected account actually kept.
ADVISOR_NET_EARNINGS = (
    Transaction.advisor_payout_usd - Transaction.stripe_fee_usd - Transaction.advisor_reversed_usd
)

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
    return round(
        as_float(txn.advisor_payout_usd)
        - as_float(txn.stripe_fee_usd)
        - as_float(txn.advisor_reversed_usd),
        2,
    )


def consultation_usd(txn: Transaction) -> float:
    """The consultation price: what the seeker paid less the tax on it."""
    return round(as_float(txn.amount_usd) - as_float(txn.tax_usd), 2)
