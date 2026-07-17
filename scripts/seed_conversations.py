"""Seed dummy chat conversations + messages for local/admin UI testing.

Requires seed advisors and seekers (run those first if missing)::

    uv run python -m scripts.seed_advisors
    uv run python -m scripts.seed_seekers
    uv run python -m scripts.seed_conversations

Idempotent: re-running replaces messages on each seed conversation thread
but keeps the same seeker/advisor pairs. Shared password: TestPass123!
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import async_session_factory, engine
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.review import ModerationStatus
from app.models.user import User, UserRole
from app.services import booking_service

logger = get_logger(__name__)

DEFAULT_PASSWORD = "TestPass123!"

# (seeker_email, advisor_email, thread messages as (from_role, body, minutes_ago, read))
THREADS: list[tuple[str, str, list[tuple[str, str, int, bool]]]] = [
    (
        "seeker1.seed@globlejump.test",
        "advisor1.seed@globlejump.test",
        [
            (
                "seeker",
                "Hi Sarah — I completed my Express Entry profile. Can we review CRS?",
                120,
                True,
            ),
            ("advisor", "Absolutely. Share your latest CRS estimate and NOC code.", 110, True),
            ("seeker", "CRS is 468, NOC 21231. I have 1 year Canadian experience.", 95, True),
            ("advisor", "Solid base. Let's book a full document review this week.", 80, True),
            ("seeker", "Great — Thursday afternoon works for me.", 60, False),
        ],
    ),
    (
        "seeker2.seed@globlejump.test",
        "advisor2.seed@globlejump.test",
        [
            ("seeker", "Hello James, I need help with an H-1B transfer timeline.", 200, True),
            ("advisor", "Happy to help. When does your current status expire?", 180, True),
            ("seeker", "End of September. Employer already started LCA.", 150, True),
            ("advisor", "We should file premium processing. I'll send a checklist.", 140, False),
        ],
    ),
    (
        "seeker3.seed@globlejump.test",
        "advisor3.seed@globlejump.test",
        [
            ("seeker", "Priya, quick question on UK Skilled Worker maintenance funds.", 90, True),
            (
                "advisor",
                "You'll need £1,270 if your sponsor doesn't certify maintenance.",
                70,
                True,
            ),
            ("seeker", "Got it — bank statements ready. Thanks!", 45, True),
            ("advisor", "Perfect. Upload them before our session.", 30, False),
            ("seeker", "Uploaded just now.", 15, False),
            ("advisor", "Received — reviewing today.", 5, False),
        ],
    ),
]


async def _get_user(session: AsyncSession, email: str, role: UserRole) -> User | None:
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or user.role != role:
        return None
    return user


async def _ensure_booking(
    session: AsyncSession, seeker: User, advisor: User
) -> Booking:
    existing = await session.scalar(
        select(Booking)
        .where(Booking.seeker_id == seeker.id)
        .where(Booking.advisor_id == advisor.id)
        .limit(1)
    )
    if existing is not None:
        return existing

    start = datetime.now(UTC) + timedelta(days=3)
    end = start + timedelta(minutes=30)
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="consultation",
        duration_minutes=30,
        price_usd=75.0,
        scheduled_start=start,
        scheduled_end=end,
        status=BookingStatus.confirmed,
        payment_status=PaymentStatus.paid,
        seeker_note="Seed chat booking",
        created_by=seeker.id,
    )
    session.add(booking)
    await session.flush()
    logger.info("chat_booking_seeded", seeker=seeker.email, advisor=advisor.email)
    return booking


async def _ensure_conversation(
    session: AsyncSession, seeker: User, advisor: User
) -> Conversation:
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.seeker_id == seeker.id)
        .where(Conversation.advisor_id == advisor.id)
    )
    if conversation is not None:
        return conversation

    conversation = Conversation(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        created_by=seeker.id,
    )
    session.add(conversation)
    await session.flush()
    logger.info("chat_conversation_created", seeker=seeker.email, advisor=advisor.email)
    return conversation


async def _replace_messages(
    session: AsyncSession,
    conversation: Conversation,
    seeker: User,
    advisor: User,
    thread: list[tuple[str, str, int, bool]],
) -> int:
    existing = (
        await session.execute(
            select(Message).where(Message.conversation_id == conversation.id)
        )
    ).scalars().all()
    for msg in existing:
        await session.delete(msg)
    await session.flush()

    last_at: datetime | None = None
    for role, body, minutes_ago, is_read in thread:
        created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        sender = seeker if role == "seeker" else advisor
        # Recipient has read seeker→advisor messages when is_read; reverse for advisor msgs.
        read_at = created + timedelta(minutes=2) if is_read else None
        message = Message(
            conversation_id=conversation.id,
            sender_id=sender.id,
            body=body,
            read_at=read_at,
            moderation_status=ModerationStatus.visible,
            created_by=sender.id,
        )
        message.created_at = created
        message.updated_at = created
        session.add(message)
        last_at = created

    conversation.last_message_at = last_at
    session.add(conversation)
    await session.flush()
    return len(thread)


async def seed_conversations() -> list[str]:
    summaries: list[str] = []
    async with async_session_factory() as session:
        for seeker_email, advisor_email, thread in THREADS:
            seeker = await _get_user(session, seeker_email, UserRole.seeker)
            advisor = await _get_user(session, advisor_email, UserRole.advisor)
            if seeker is None or advisor is None:
                missing = []
                if seeker is None:
                    missing.append(seeker_email)
                if advisor is None:
                    missing.append(advisor_email)
                msg = f"SKIP — missing users: {', '.join(missing)}"
                logger.warning("chat_seed_skipped", detail=msg)
                summaries.append(msg)
                continue

            await _ensure_booking(session, seeker, advisor)
            conversation = await _ensure_conversation(session, seeker, advisor)
            count = await _replace_messages(session, conversation, seeker, advisor, thread)
            line = (
                f"{seeker.full_name} ↔ {advisor.full_name} "
                f"({count} messages) conversation_id={conversation.id}"
            )
            summaries.append(line)
            logger.info("chat_thread_seeded", conversation_id=str(conversation.id), messages=count)

        await session.commit()
    return summaries


async def main() -> None:
    password = os.environ.get("SEED_ADVISOR_PASSWORD", DEFAULT_PASSWORD)
    try:
        summaries = await seed_conversations()
    finally:
        await engine.dispose()

    print("Chat seed results:")
    for line in summaries:
        print(f"  - {line}")
    print()
    print("Login as any seed seeker/advisor with password:", password)
    print("Examples: seeker1.seed@globlejump.test / advisor1.seed@globlejump.test")
    if any(s.startswith("SKIP") for s in summaries):
        print()
        print("If users were missing, run:")
        print("  uv run python -m scripts.seed_advisors")
        print("  uv run python -m scripts.seed_seekers")
        print("  uv run python -m scripts.seed_conversations")


if __name__ == "__main__":
    asyncio.run(main())
