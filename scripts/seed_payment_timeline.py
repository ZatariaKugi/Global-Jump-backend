"""Seed admin payment Timeline, Logs, and Invoice demo data.

Creates 3 transactions with full ``transaction_events`` histories and
``invoice_number`` on paid/refunded rows so::

    GET /api/v1/admin/payments/{txn_id}/invoice
    GET /api/v1/admin/payments/{txn_id}/timeline
    GET /api/v1/admin/payments/{txn_id}/logs

work out of the box.

  - succeeded (initiated → … → closed) + invoice
  - refunded  (happy path + refunded + closed) + invoice
  - failed    (initiated → failed → closed) — no invoice

Also backfills events onto an existing transaction when
``SEED_TIMELINE_TXN_ID`` is set (or ``--txn-id``).

Run with::

    uv run python -m scripts.seed_payment_timeline
    uv run python -m scripts.seed_payment_timeline \\
        --txn-id a85440af-ae9e-4710-86f7-5889f1d5643e

Idempotent for seed emails. Password: TestPass123!
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.transaction_event import TransactionEvent, TransactionEventType
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
EMAIL_PREFIX = "payment.timeline.seed."
SEEKER_EMAIL = f"{EMAIL_PREFIX}seeker@globlejump.test"
ADVISOR_EMAIL = f"{EMAIL_PREFIX}advisor@globlejump.test"

COMMISSION_RATE = 0.15
TAX_RATE = 0.08

# Happy-path steps after initiated (before closed).
_SUCCESS_STEPS: tuple[TransactionEventType, ...] = (
    TransactionEventType.authorized,
    TransactionEventType.completed,
    TransactionEventType.invoice_generated,
    TransactionEventType.receipt_sent,
)


async def _clear_prior(session: AsyncSession) -> int:
    users = (
        (await session.execute(select(User).where(User.email.like(f"{EMAIL_PREFIX}%"))))
        .scalars()
        .all()
    )
    if not users:
        return 0
    ids = [u.id for u in users]
    booking_ids = (
        (
            await session.execute(
                select(Booking.id).where(
                    Booking.advisor_id.in_(ids) | Booking.seeker_id.in_(ids)
                )
            )
        )
        .scalars()
        .all()
    )
    if booking_ids:
        txn_ids = (
            (
                await session.execute(
                    select(Transaction.id).where(Transaction.booking_id.in_(booking_ids))
                )
            )
            .scalars()
            .all()
        )
        if txn_ids:
            await session.execute(
                delete(TransactionEvent).where(TransactionEvent.transaction_id.in_(txn_ids))
            )
        await session.execute(delete(Transaction).where(Transaction.booking_id.in_(booking_ids)))
        await session.execute(delete(Booking).where(Booking.id.in_(booking_ids)))
    await session.execute(delete(SeekerProfile).where(SeekerProfile.user_id.in_(ids)))
    await session.execute(delete(AdvisorProfile).where(AdvisorProfile.user_id.in_(ids)))
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
        email_verified_at=datetime.now(UTC) - timedelta(days=30),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()
    if role == UserRole.seeker:
        session.add(
            SeekerProfile(
                user_id=user.id,
                country_of_residence="CA",
                nationality="PK",
                intended_visa_type="study",
                intended_destination="CA",
            )
        )
    elif role == UserRole.advisor:
        session.add(
            AdvisorProfile(
                user_id=user.id,
                title="Immigration Consultant",
                country_of_residence="CA",
                years_of_experience=8,
            )
        )
    await session.flush()
    return user


async def _add_events(
    session: AsyncSession,
    txn_id: uuid.UUID,
    steps: list[tuple[TransactionEventType, datetime]],
) -> int:
    await session.execute(
        delete(TransactionEvent).where(TransactionEvent.transaction_id == txn_id)
    )
    for event_type, when in steps:
        session.add(
            TransactionEvent(
                transaction_id=txn_id,
                event_type=event_type,
                occurred_at=when,
            )
        )
    await session.flush()
    return len(steps)


def _happy_path_steps(start: datetime) -> list[tuple[TransactionEventType, datetime]]:
    steps: list[tuple[TransactionEventType, datetime]] = [
        (TransactionEventType.initiated, start),
    ]
    cursor = start
    for event_type in _SUCCESS_STEPS:
        cursor += timedelta(seconds=45)
        steps.append((event_type, cursor))
    steps.append((TransactionEventType.closed, cursor + timedelta(seconds=20)))
    return steps


def _refund_path_steps(start: datetime) -> list[tuple[TransactionEventType, datetime]]:
    steps = _happy_path_steps(start)
    refund_at = steps[-1][1] + timedelta(days=1, hours=2)
    steps.append((TransactionEventType.refunded, refund_at))
    steps.append((TransactionEventType.closed, refund_at + timedelta(seconds=15)))
    return steps


def _failed_path_steps(start: datetime) -> list[tuple[TransactionEventType, datetime]]:
    return [
        (TransactionEventType.initiated, start),
        (TransactionEventType.failed, start + timedelta(hours=1)),
        (TransactionEventType.closed, start + timedelta(hours=1, seconds=10)),
    ]


async def _create_txn(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    when: datetime,
    amount_usd: float,
    status: TransactionStatus,
    payment_status: PaymentStatus,
    tag: str,
    refunded: bool = False,
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
        scheduled_start=when + timedelta(days=3),
        scheduled_end=when + timedelta(days=3, minutes=45),
        status=BookingStatus.confirmed,
        payment_status=payment_status,
        created_by=seeker.id,
    )
    session.add(booking)
    await session.flush()

    max_invoice = await session.scalar(select(func.max(Transaction.invoice_number)))
    invoice_number = (
        (max_invoice or 3000) + 1
        if status
        in (
            TransactionStatus.succeeded,
            TransactionStatus.refunded,
            TransactionStatus.partially_refunded,
        )
        else None
    )

    txn = Transaction(
        booking_id=booking.id,
        stripe_checkout_session_id=f"cs_tl_{tag}_{uuid.uuid4().hex[:10]}",
        stripe_payment_intent_id=(
            None if status == TransactionStatus.pending else f"pi_tl_{tag}_{uuid.uuid4().hex[:10]}"
        ),
        stripe_charge_id=(
            None
            if status in (TransactionStatus.pending, TransactionStatus.failed)
            else f"ch_tl_{tag}_{uuid.uuid4().hex[:10]}"
        ),
        amount_usd=amount_usd,
        commission_rate=COMMISSION_RATE,
        commission_usd=commission_usd,
        tax_rate=TAX_RATE,
        tax_usd=tax_usd,
        advisor_payout_usd=advisor_payout_usd,
        status=status,
        invoice_number=invoice_number,
        refunded_at=when + timedelta(days=1, hours=2) if refunded else None,
        refunded_amount_usd=amount_usd if refunded else None,
        refund_reason="Seed timeline demo refund" if refunded else None,
        created_by=seeker.id,
    )
    txn.created_at = when
    session.add(txn)
    await session.flush()
    return txn


async def backfill_existing_txn(
    session: AsyncSession, txn_id: uuid.UUID
) -> tuple[bool, int, str]:
    txn = await session.get(Transaction, txn_id)
    if txn is None:
        return False, 0, "transaction_not_found"

    if (
        txn.status
        in (
            TransactionStatus.succeeded,
            TransactionStatus.refunded,
            TransactionStatus.partially_refunded,
        )
        and txn.invoice_number is None
    ):
        max_invoice = await session.scalar(select(func.max(Transaction.invoice_number)))
        txn.invoice_number = (max_invoice or 3000) + 1
        session.add(txn)
        await session.flush()

    start = txn.created_at if txn.created_at.tzinfo else txn.created_at.replace(tzinfo=UTC)
    if txn.status == TransactionStatus.failed:
        steps = _failed_path_steps(start)
    elif txn.status in (TransactionStatus.refunded, TransactionStatus.partially_refunded):
        steps = _refund_path_steps(start)
    elif txn.status == TransactionStatus.pending:
        steps = [(TransactionEventType.initiated, start)]
    else:
        steps = _happy_path_steps(start)

    n = await _add_events(session, txn.id, steps)
    detail = txn.status.value
    if txn.invoice_number is not None:
        detail = f"{detail},invoice=INV-{txn.invoice_number:08d}"
    return True, n, detail


async def seed_payment_timeline(
    *, backfill_txn_id: uuid.UUID | None = None
) -> list[str]:
    lines: list[str] = []
    password_hash = hash_password(PASSWORD)
    now = datetime.now(UTC)

    async with async_session_factory() as session:
        if backfill_txn_id is not None:
            ok, n, detail = await backfill_existing_txn(session, backfill_txn_id)
            if ok:
                await session.commit()
                lines.append(f"backfilled_txn={backfill_txn_id} events={n} status={detail}")
            else:
                lines.append(f"backfill_skipped txn={backfill_txn_id} reason={detail}")

        cleared = await _clear_prior(session)
        lines.append(f"cleared_prior_users={cleared}")

        seeker = await _make_user(
            session,
            email=SEEKER_EMAIL,
            full_name="Timeline Seed Seeker",
            role=UserRole.seeker,
            password_hash=password_hash,
        )
        advisor = await _make_user(
            session,
            email=ADVISOR_EMAIL,
            full_name="Timeline Seed Advisor",
            role=UserRole.advisor,
            password_hash=password_hash,
        )

        succeeded = await _create_txn(
            session,
            seeker=seeker,
            advisor=advisor,
            when=now - timedelta(days=2),
            amount_usd=149.0,
            status=TransactionStatus.succeeded,
            payment_status=PaymentStatus.paid,
            tag="ok",
        )
        n_ok = await _add_events(
            session, succeeded.id, _happy_path_steps(now - timedelta(days=2))
        )
        lines.append(f"succeeded_txn={succeeded.id} events={n_ok}")

        refunded = await _create_txn(
            session,
            seeker=seeker,
            advisor=advisor,
            when=now - timedelta(days=5),
            amount_usd=99.0,
            status=TransactionStatus.refunded,
            payment_status=PaymentStatus.refunded,
            tag="rf",
            refunded=True,
        )
        n_rf = await _add_events(
            session, refunded.id, _refund_path_steps(now - timedelta(days=5))
        )
        lines.append(f"refunded_txn={refunded.id} events={n_rf}")

        failed = await _create_txn(
            session,
            seeker=seeker,
            advisor=advisor,
            when=now - timedelta(hours=6),
            amount_usd=75.0,
            status=TransactionStatus.failed,
            payment_status=PaymentStatus.unpaid,
            tag="fl",
        )
        n_fl = await _add_events(
            session, failed.id, _failed_path_steps(now - timedelta(hours=6))
        )
        lines.append(f"failed_txn={failed.id} events={n_fl}")

        await session.commit()
        lines.append(f"seeker={SEEKER_EMAIL}")
        lines.append(f"advisor={ADVISOR_EMAIL}")
        lines.append(f"password={PASSWORD}")
        lines.append(
            f"invoice_url=/api/v1/admin/payments/{succeeded.id}/invoice "
            f"(INV-{succeeded.invoice_number:08d})"
        )
        lines.append(
            f"invoice_url=/api/v1/admin/payments/{refunded.id}/invoice "
            f"(INV-{refunded.invoice_number:08d})"
        )
        lines.append(
            f"timeline_url=/api/v1/admin/payments/{succeeded.id}/timeline"
        )
        lines.append(f"logs_url=/api/v1/admin/payments/{succeeded.id}/logs")
    return lines


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--txn-id",
        default=os.environ.get("SEED_TIMELINE_TXN_ID"),
        help="Optional existing transaction UUID to backfill timeline events onto",
    )
    args = parser.parse_args()
    backfill_id = uuid.UUID(args.txn_id) if args.txn_id else None

    try:
        for line in await seed_payment_timeline(backfill_txn_id=backfill_id):
            print(line)
        print()
        print("GET /api/v1/admin/payments/{txn_id}/invoice")
        print("GET /api/v1/admin/payments/{txn_id}/timeline")
        print("GET /api/v1/admin/payments/{txn_id}/logs")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
