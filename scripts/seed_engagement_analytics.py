"""Seed data for the admin Engagement Analytics tab (~6 months + prior baseline).

Populates ``GET /api/v1/admin/analytics/engagement`` with:
  - messages_sent / video_call_hours / documents_uploaded card totals
  - ``*_change_pct`` vs the prior window (older months act as baseline)
  - messages_sent_trend / video_call_hours_trend / documents_uploaded_trend
    as ``{month, value}``

Run with::

    uv run python -m scripts.seed_engagement_analytics

Idempotent: deletes prior ``engagement.analytics.seed.*`` users (and cascaded
rows), then recreates. Password: TestPass123!

Use ``?days=180`` so the 6-month trends appear (endpoint default is 30 days).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.review import ModerationStatus
from app.models.seeker_document import DocumentCategory, SeekerDocument, SeekerDocumentStatus
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
EMAIL_PREFIX = "engagement.analytics.seed."
SEEKER_EMAIL = f"{EMAIL_PREFIX}seeker@globlejump.test"
ADVISOR_EMAIL = f"{EMAIL_PREFIX}advisor@globlejump.test"

_DOC_CATEGORIES = (
    DocumentCategory.passport,
    DocumentCategory.educational,
    DocumentCategory.finance,
    DocumentCategory.supporting,
)

# months_ago → (messages, completed_sessions, duration_minutes, documents)
# Index 0 = current month … 5 = five months ago (6 months of trend).
MONTHLY: list[tuple[int, int, int, int]] = [
    (480, 32, 60, 110),  # 0 current
    (420, 28, 60, 95),  # 1
    (360, 24, 55, 80),  # 2
    (310, 20, 55, 68),  # 3
    (270, 18, 50, 55),  # 4
    (230, 14, 50, 45),  # 5
]

# Extra older baseline (~6–8 months ago) so ?days=180 change_pct ≠ 100%.
PRIOR_BASELINE: list[tuple[int, int, int, int]] = [
    (190, 12, 45, 38),  # 6
    (160, 10, 45, 30),  # 7
    (140, 8, 40, 25),  # 8
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

    conversations = (
        (
            await session.execute(
                select(Conversation).where(
                    Conversation.seeker_id.in_(ids) | Conversation.advisor_id.in_(ids)
                )
            )
        )
        .scalars()
        .all()
    )
    conv_ids = [c.id for c in conversations]
    if conv_ids:
        await session.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))

    await session.execute(delete(SeekerDocument).where(SeekerDocument.seeker_id.in_(ids)))
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
        email_verified_at=datetime.now(UTC) - timedelta(days=300),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()
    return user


async def _add_messages(
    session: AsyncSession,
    *,
    conversation: Conversation,
    seeker: User,
    advisor: User,
    count: int,
    anchor: datetime,
) -> None:
    for i in range(count):
        sender = seeker if i % 2 == 0 else advisor
        created = anchor + timedelta(minutes=i * 3)
        message = Message(
            conversation_id=conversation.id,
            sender_id=sender.id,
            body=f"Seed engagement message {i + 1}",
            moderation_status=ModerationStatus.visible,
            created_by=sender.id,
        )
        message.created_at = created
        message.updated_at = created
        session.add(message)
    conversation.last_message_at = anchor + timedelta(minutes=max(count - 1, 0) * 3)
    session.add(conversation)


async def _add_sessions(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    count: int,
    duration_minutes: int,
    anchor: datetime,
) -> None:
    for i in range(count):
        scheduled = anchor + timedelta(hours=i)
        booking = Booking(
            seeker_id=seeker.id,
            advisor_id=advisor.id,
            appointment_number=await booking_service._next_appointment_number(session),
            service_type="immigration_specialist",
            duration_minutes=duration_minutes,
            price_usd=99.0,
            scheduled_start=scheduled,
            scheduled_end=scheduled + timedelta(minutes=duration_minutes),
            status=BookingStatus.completed,
            payment_status=PaymentStatus.paid,
        )
        session.add(booking)
        await session.flush()


async def _add_documents(
    session: AsyncSession,
    *,
    seeker: User,
    count: int,
    anchor: datetime,
) -> None:
    for i in range(count):
        category = _DOC_CATEGORIES[i % len(_DOC_CATEGORIES)]
        created = anchor + timedelta(hours=i)
        doc = SeekerDocument(
            seeker_id=seeker.id,
            category=category,
            document_name=f"{category.value}_{i + 1}.pdf",
            file_url=f"/uploads/seed/engagement/{seeker.id}/{category.value}_{i}.pdf",
            file_size_bytes=100_000 + i * 500,
            content_type="application/pdf",
            status=SeekerDocumentStatus.under_review,
            created_by=seeker.id,
        )
        session.add(doc)
        await session.flush()
        doc.created_at = created
        doc.updated_at = created
        session.add(doc)


async def _seed_month(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    conversation: Conversation,
    months_ago: int,
    messages: int,
    sessions: int,
    duration_minutes: int,
    documents: int,
) -> tuple[int, float, int]:
    anchor = _month_anchor(months_ago)
    await _add_messages(
        session,
        conversation=conversation,
        seeker=seeker,
        advisor=advisor,
        count=messages,
        anchor=anchor,
    )
    await _add_sessions(
        session,
        seeker=seeker,
        advisor=advisor,
        count=sessions,
        duration_minutes=duration_minutes,
        anchor=anchor,
    )
    await _add_documents(session, seeker=seeker, count=documents, anchor=anchor)
    hours = round(sessions * duration_minutes / 60.0, 2)
    return messages, hours, documents


async def seed_engagement_analytics() -> list[str]:
    lines: list[str] = []
    password_hash = hash_password(PASSWORD)

    async with async_session_factory() as session:
        cleared = await _clear_prior(session)
        lines.append(f"cleared_prior_users={cleared}")

        seeker = await _make_user(
            session,
            email=SEEKER_EMAIL,
            full_name="Engagement Analytics Seed Seeker",
            role=UserRole.seeker,
            password_hash=password_hash,
        )
        advisor = await _make_user(
            session,
            email=ADVISOR_EMAIL,
            full_name="Engagement Analytics Seed Advisor",
            role=UserRole.advisor,
            password_hash=password_hash,
        )

        conversation = Conversation(
            seeker_id=seeker.id,
            advisor_id=advisor.id,
            created_by=seeker.id,
        )
        session.add(conversation)
        await session.flush()

        total_messages = 0
        total_hours = 0.0
        total_documents = 0

        for months_ago, (messages, sessions, duration, documents) in enumerate(MONTHLY):
            msg_n, hours, doc_n = await _seed_month(
                session,
                seeker=seeker,
                advisor=advisor,
                conversation=conversation,
                months_ago=months_ago,
                messages=messages,
                sessions=sessions,
                duration_minutes=duration,
                documents=documents,
            )
            total_messages += msg_n
            total_hours += hours
            total_documents += doc_n
            month_key = _month_anchor(months_ago).strftime("%Y-%m")
            lines.append(
                f"month={month_key} messages={msg_n} video_hours={hours} documents={doc_n}"
            )

        for i, (messages, sessions, duration, documents) in enumerate(PRIOR_BASELINE):
            months_ago = len(MONTHLY) + i
            msg_n, hours, doc_n = await _seed_month(
                session,
                seeker=seeker,
                advisor=advisor,
                conversation=conversation,
                months_ago=months_ago,
                messages=messages,
                sessions=sessions,
                duration_minutes=duration,
                documents=documents,
            )
            total_messages += msg_n
            total_hours += hours
            total_documents += doc_n
            month_key = _month_anchor(months_ago).strftime("%Y-%m")
            lines.append(
                f"baseline={month_key} messages={msg_n} video_hours={hours} documents={doc_n}"
            )

        await session.commit()
        lines.append(f"seeker={SEEKER_EMAIL}")
        lines.append(f"advisor={ADVISOR_EMAIL}")
        lines.append(f"messages={total_messages}")
        lines.append(f"video_call_hours={round(total_hours, 2)}")
        lines.append(f"documents={total_documents}")
        lines.append(f"password={PASSWORD}")
    return lines


async def main() -> None:
    try:
        for line in await seed_engagement_analytics():
            print(line)
        print()
        print("Engagement Analytics: GET /api/v1/admin/analytics/engagement?days=180")
        print("  (default days=30 only covers ~1 month of trends)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
