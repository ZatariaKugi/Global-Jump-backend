"""Background scheduler for the delayed Connect payout sweep.

A single ``AsyncIOScheduler`` runs ``payment_service.run_due_transfers`` on a fixed
interval. The due-state lives in Postgres (``transfer_after`` timestamps), so the job
is safe across restarts and safe to run from multiple app instances — the sweep locks
due rows ``FOR UPDATE SKIP LOCKED`` and uses Stripe idempotency keys.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services import booking_service, payment_service, push_service

logger = get_logger(__name__)


async def _sweep_due_transfers(settings: Settings) -> None:
    """One sweep pass. Opens its own session and commits/rolls back explicitly."""
    if not settings.STRIPE_SECRET_KEY:
        return  # payments not configured (e.g. local dev without Stripe) — nothing to do
    async with async_session_factory() as session:
        try:
            count = await payment_service.run_due_transfers(session, settings)
            await session.commit()
            if count:
                logger.info("transfer_sweep_completed", transfers=count)
        except Exception:  # noqa: BLE001 — a sweep failure must not kill the scheduler
            await session.rollback()
            logger.exception("transfer_sweep_failed")


async def _sweep_expired_pending_bookings(settings: Settings) -> None:
    """Cancel pending bookings past their start time; refund paid ones."""
    async with async_session_factory() as session:
        try:
            count = await booking_service.expire_unaccepted_pending_bookings(session, settings)
            await session.commit()
            if count:
                logger.info("booking_expiry_sweep_completed", expired=count)
        except Exception:  # noqa: BLE001 — a sweep failure must not kill the scheduler
            await session.rollback()
            logger.exception("booking_expiry_sweep_failed")


async def _sweep_due_pushes(settings: Settings) -> None:
    """One push-outbox pass. Opens its own session and commits/rolls back explicitly."""
    async with async_session_factory() as session:
        try:
            count = await push_service.run_due_pushes(session, settings)
            await session.commit()
            if count:
                logger.info("push_sweep_completed", pushes=count)
        except Exception:  # noqa: BLE001 — a sweep failure must not kill the scheduler
            await session.rollback()
            logger.exception("push_sweep_failed")


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Build the scheduler with the transfer-sweep job registered."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _sweep_due_transfers,
        trigger="interval",
        seconds=settings.TRANSFER_SWEEP_SECONDS,
        args=[settings],
        id="transfer_sweep",
        # If the app was down across several intervals, collapse the backlog into one
        # run and tolerate a late start rather than firing many catch-up passes.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=settings.TRANSFER_SWEEP_SECONDS,
    )
    scheduler.add_job(
        _sweep_expired_pending_bookings,
        trigger="interval",
        seconds=settings.BOOKING_EXPIRY_SWEEP_SECONDS,
        args=[settings],
        id="booking_expiry_sweep",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=settings.BOOKING_EXPIRY_SWEEP_SECONDS,
    )
    scheduler.add_job(
        _sweep_due_pushes,
        trigger="interval",
        seconds=settings.NOTIFICATION_PUSH_SWEEP_SECONDS,
        args=[settings],
        id="notification_push_sweep",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=settings.NOTIFICATION_PUSH_SWEEP_SECONDS,
    )
    return scheduler
