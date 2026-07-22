"""Seed data for the admin Finance Analytics tab (~8 months).

Populates ``GET /api/v1/admin/analytics/finance?days=365`` with:
  - gross / net / refunds / advisor_payout card totals
  - ``*_change_pct`` vs the prior window (older months act as baseline)
  - revenue_trend / refund_trend / monthly_payouts as ``{month, amount_usd}``

Run with::

    uv run python -m scripts.seed_finance_analytics

Idempotent: deletes prior ``finance.analytics.seed.*`` users (and cascaded
rows), then recreates. Password: TestPass123!
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.payout_request import PayoutMethod, PayoutRequest, PayoutStatus
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
EMAIL_PREFIX = "finance.analytics.seed."
SEEKER_EMAIL = f"{EMAIL_PREFIX}seeker@globlejump.test"
ADVISOR_EMAIL = f"{EMAIL_PREFIX}advisor@globlejump.test"

COMMISSION_RATE = 0.15
TAX_RATE = 0.08

# months_ago → (gross_txn_count, avg_amount, refund_count, refund_avg, payout_usd)
# Index 0 = current month … 7 = seven months ago (8 months total).
MONTHLY: list[tuple[int, float, int, float, float]] = [
    (18, 149.0, 2, 60.0, 920.0),  # 0 current
    (16, 139.0, 1, 80.0, 840.0),  # 1
    (15, 129.0, 2, 55.0, 780.0),  # 2
    (14, 119.0, 1, 70.0, 710.0),  # 3
    (12, 109.0, 2, 45.0, 640.0),  # 4
    (11, 99.0, 1, 50.0, 580.0),  # 5
    (10, 95.0, 1, 40.0, 520.0),  # 6
    (9, 89.0, 1, 35.0, 460.0),  # 7
]

# Extra older baseline (~9–14 months ago) so ?days=180/240 change_pct ≠ 100%.
PRIOR_BASELINE: list[tuple[int, float, int, float, float]] = [
    (8, 85.0, 1, 30.0, 400.0),
    (7, 80.0, 1, 25.0, 360.0),
    (6, 75.0, 0, 0.0, 320.0),
]


def _month_anchor(months_ago: int) -> datetime:
    now = datetime.now(UTC)
    year, month = now.year, now.month - months_ago
    while month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 12, 14, 0, tzinfo=UTC)


async def _clear_prior(session: AsyncSession) -> int:
    users = (
        (await session.execute(select(User).where(User.email.like(f"{EMAIL_PREFIX}%"))))
        .scalars()
        .all()
    )
    if not users:
        return 0
    ids = [u.id for u in users]
    await session.execute(
        delete(PayoutRequest).where(PayoutRequest.advisor_id.in_(ids))
    )
    await session.execute(
        delete(Booking).where(Booking.advisor_id.in_(ids) | Booking.seeker_id.in_(ids))
    )
    await session.execute(delete(User).where(User.id.in_(ids)))
    await session.flush()
    return len(ids)


async def _make_user(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    role: UserRole,
    password_hash: str,
) -> User:
    user = User(
        email=email,
        full_name=full_name,
        hashed_password=password_hash,
        role=role,
        is_active=True,
        email_verified_at=datetime.now(UTC) - timedelta(days=400),
        verification_status=VerificationStatus.approved,
    )
    user.created_at = datetime.now(UTC) - timedelta(days=400)
    session.add(user)
    await session.flush()
    return user


async def _add_paid_booking_txn(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    when: datetime,
    amount_usd: float,
    status: TransactionStatus = TransactionStatus.succeeded,
    refunded_at: datetime | None = None,
    refunded_amount_usd: float | None = None,
) -> Transaction:
    commission_usd = round(amount_usd * COMMISSION_RATE, 2)
    tax_usd = round(amount_usd * TAX_RATE, 2)
    advisor_payout_usd = round(amount_usd - commission_usd - tax_usd, 2)

    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="immigration_specialist",
        duration_minutes=45,
        price_usd=amount_usd,
        scheduled_start=when,
        scheduled_end=when + timedelta(minutes=45),
        status=BookingStatus.completed,
        payment_status=(
            PaymentStatus.refunded if refunded_at is not None else PaymentStatus.paid
        ),
    )
    session.add(booking)
    await session.flush()

    max_invoice = await session.scalar(select(func.max(Transaction.invoice_number)))
    invoice_number = (max_invoice or 2000) + 1

    txn = Transaction(
        booking_id=booking.id,
        stripe_checkout_session_id=f"cs_fin_{uuid.uuid4().hex[:14]}",
        stripe_payment_intent_id=f"pi_fin_{uuid.uuid4().hex[:14]}",
        stripe_charge_id=f"ch_fin_{uuid.uuid4().hex[:14]}",
        amount_usd=amount_usd,
        commission_rate=COMMISSION_RATE,
        commission_usd=commission_usd,
        tax_rate=TAX_RATE,
        tax_usd=tax_usd,
        advisor_payout_usd=advisor_payout_usd,
        status=status,
        invoice_number=invoice_number,
        refunded_at=refunded_at,
        refunded_amount_usd=refunded_amount_usd,
        refund_reason="Seed finance analytics refund" if refunded_at else None,
    )
    txn.created_at = when
    session.add(txn)
    await session.flush()
    return txn


async def _add_payout(
    session: AsyncSession, *, advisor: User, amount_usd: float, processed_at: datetime
) -> None:
    fee = round(amount_usd * 0.02, 2)
    payout = PayoutRequest(
        advisor_id=advisor.id,
        amount_usd=amount_usd,
        method=PayoutMethod.bank_transfer,
        processing_fee_usd=fee,
        net_amount_usd=round(amount_usd - fee, 2),
        status=PayoutStatus.completed,
        processed_at=processed_at,
        account_holder_name=advisor.full_name,
        bank_name="Seed Bank",
    )
    payout.created_at = processed_at - timedelta(days=2)
    session.add(payout)
    await session.flush()


async def _seed_month(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    months_ago: int,
    txn_count: int,
    avg_amount: float,
    refund_count: int,
    refund_avg: float,
    payout_usd: float,
) -> tuple[float, float, float]:
    anchor = _month_anchor(months_ago)
    gross = 0.0
    refunds = 0.0

    for i in range(txn_count):
        amount = round(avg_amount + (i % 5) * 10.0, 2)
        when = anchor + timedelta(hours=i * 3)
        await _add_paid_booking_txn(
            session,
            seeker=seeker,
            advisor=advisor,
            when=when,
            amount_usd=amount,
        )
        gross += amount

    for i in range(refund_count):
        amount = round(avg_amount + 20.0, 2)
        refund_amt = round(refund_avg + i * 5.0, 2)
        when = anchor + timedelta(days=1, hours=i * 2)
        refunded_at = when + timedelta(days=3 + i)
        await _add_paid_booking_txn(
            session,
            seeker=seeker,
            advisor=advisor,
            when=when,
            amount_usd=amount,
            status=(
                TransactionStatus.refunded
                if refund_amt >= amount
                else TransactionStatus.partially_refunded
            ),
            refunded_at=refunded_at,
            refunded_amount_usd=min(refund_amt, amount),
        )
        # Gross includes refunded/partially_refunded statuses.
        gross += amount
        refunds += min(refund_amt, amount)

    if payout_usd > 0:
        await _add_payout(
            session,
            advisor=advisor,
            amount_usd=payout_usd,
            processed_at=anchor.replace(day=18),
        )

    return gross, refunds, payout_usd


async def seed_finance_analytics() -> list[str]:
    lines: list[str] = []
    password_hash = hash_password(PASSWORD)

    async with async_session_factory() as session:
        cleared = await _clear_prior(session)
        lines.append(f"cleared_prior_users={cleared}")

        seeker = await _make_user(
            session,
            email=SEEKER_EMAIL,
            full_name="Finance Analytics Seeker",
            role=UserRole.seeker,
            password_hash=password_hash,
        )
        advisor = await _make_user(
            session,
            email=ADVISOR_EMAIL,
            full_name="Finance Analytics Advisor",
            role=UserRole.advisor,
            password_hash=password_hash,
        )

        total_gross = 0.0
        total_refunds = 0.0
        total_payouts = 0.0
        months_seeded = 0

        for months_ago, (n, avg, r_n, r_avg, payout) in enumerate(MONTHLY):
            g, r, p = await _seed_month(
                session,
                seeker=seeker,
                advisor=advisor,
                months_ago=months_ago,
                txn_count=n,
                avg_amount=avg,
                refund_count=r_n,
                refund_avg=r_avg,
                payout_usd=payout,
            )
            total_gross += g
            total_refunds += r
            total_payouts += p
            months_seeded += 1
            ym = _month_anchor(months_ago).strftime("%Y-%m")
            lines.append(
                f"month={ym} gross≈{round(g, 2)} refunds≈{round(r, 2)} payout={payout}"
            )

        for offset, (n, avg, r_n, r_avg, payout) in enumerate(PRIOR_BASELINE):
            months_ago = 8 + offset
            g, r, p = await _seed_month(
                session,
                seeker=seeker,
                advisor=advisor,
                months_ago=months_ago,
                txn_count=n,
                avg_amount=avg,
                refund_count=r_n,
                refund_avg=r_avg,
                payout_usd=payout,
            )
            total_gross += g
            total_refunds += r
            total_payouts += p
            months_seeded += 1

        await session.commit()
        lines.append(f"seeker={SEEKER_EMAIL}")
        lines.append(f"advisor={ADVISOR_EMAIL}")
        lines.append(f"months_seeded={months_seeded}")
        lines.append(f"gross_usd≈{round(total_gross, 2)}")
        lines.append(f"refunds_usd≈{round(total_refunds, 2)}")
        lines.append(f"payouts_usd≈{round(total_payouts, 2)}")
        lines.append(f"password={PASSWORD}")
    return lines


async def main() -> None:
    try:
        for line in await seed_finance_analytics():
            print(line)
        print()
        print("Finance Analytics: GET /api/v1/admin/analytics/finance?days=365")
        print("  (8 primary months + 3 older baseline months for change_pct)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
