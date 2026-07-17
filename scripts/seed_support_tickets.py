"""Seed admin support tickets + reply threads for the Verification/Support UI.

Requires seed seekers (and optionally advisors). Creates a seed admin if needed.

    uv run python -m scripts.seed_seekers
    uv run python -m scripts.seed_support_tickets

Idempotent: tickets keyed by subject — re-running resets status/messages.
Password for seed users: TestPass123! (admin: AdminPass123!)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.support_ticket import (
    SupportTicket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from app.models.ticket_message import TicketMessage
from app.models.user import User, UserRole, VerificationStatus

logger = get_logger(__name__)

SEED_ADMIN_EMAIL = "admin.seed@globlejump.test"
SEED_ADMIN_PASSWORD = "AdminPass123!"

# subject is the idempotency key
TICKETS: list[dict] = [
    {
        "subject": "[SEED] Cannot log in after password reset",
        "description": "Reset email arrived but the new password is rejected on login.",
        "user_email": "seeker1.seed@globlejump.test",
        "category": TicketCategory.account,
        "priority": TicketPriority.high,
        "status": TicketStatus.open,
        "hours_ago": 48,
        "messages": [
            ("user", "I reset my password twice — still getting Invalid credentials.", 47),
            ("admin", "Thanks — checking your account status now.", 40),
            ("user", "Appreciate it. Happy to hop on a call if needed.", 35),
        ],
    },
    {
        "subject": "[SEED] Charged twice for consultation",
        "description": "Stripe receipt shows two charges for the same booking.",
        "user_email": "seeker2.seed@globlejump.test",
        "category": TicketCategory.billing,
        "priority": TicketPriority.urgent,
        "status": TicketStatus.in_progress,
        "hours_ago": 24,
        "messages": [
            ("user", "Booking #3520000123 — charged $75 twice yesterday.", 23),
            ("admin", "We've flagged this for finance. Refund should land in 3–5 days.", 18),
        ],
    },
    {
        "subject": "[SEED] Advisor profile photo not updating",
        "description": "Upload succeeds but the marketplace card still shows the old image.",
        "user_email": "advisor1.seed@globlejump.test",
        "category": TicketCategory.technical,
        "priority": TicketPriority.medium,
        "status": TicketStatus.in_progress,
        "hours_ago": 12,
        "messages": [
            ("user", "Tried PNG and JPG under 2MB. Cache cleared.", 11),
            ("admin", "Looking into CDN cache headers for profile photos.", 6),
        ],
    },
    {
        "subject": "[SEED] Need to reschedule booking",
        "description": "Client flight delayed — need a new slot next week.",
        "user_email": "seeker3.seed@globlejump.test",
        "category": TicketCategory.booking,
        "priority": TicketPriority.low,
        "status": TicketStatus.resolved,
        "hours_ago": 72,
        "messages": [
            ("user", "Can we move Thursday's consult to Monday?", 70),
            ("admin", "Done — Monday 15:00 UTC confirmed. Closing this ticket.", 65),
        ],
    },
    {
        "subject": "[SEED] General product feedback",
        "description": "Wishlist items for the seeker dashboard.",
        "user_email": "seeker4.seed@globlejump.test",
        "category": TicketCategory.other,
        "priority": TicketPriority.low,
        "status": TicketStatus.closed,
        "hours_ago": 120,
        "messages": [
            ("user", "Would love document checklist progress on mobile.", 118),
            ("admin", "Logged for product — thanks! Closing as feedback captured.", 100),
        ],
    },
]


async def _get_or_create_admin(session: AsyncSession) -> User:
    admin = await session.scalar(select(User).where(User.email == SEED_ADMIN_EMAIL))
    if admin is not None:
        return admin
    admin = User(
        email=SEED_ADMIN_EMAIL,
        full_name="Seed Admin",
        hashed_password=hash_password(SEED_ADMIN_PASSWORD),
        role=UserRole.admin,
        is_active=True,
        email_verified_at=datetime.now(UTC),
        verification_status=VerificationStatus.approved,
    )
    session.add(admin)
    await session.flush()
    logger.info("support_seed_admin_created", email=SEED_ADMIN_EMAIL)
    return admin


async def _get_user(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email))


async def _upsert_ticket(
    session: AsyncSession, admin: User, data: dict
) -> tuple[str, str] | None:
    user = await _get_user(session, data["user_email"])
    if user is None:
        return None

    ticket = await session.scalar(
        select(SupportTicket).where(SupportTicket.subject == data["subject"])
    )
    created = datetime.now(UTC) - timedelta(hours=int(data["hours_ago"]))
    status: TicketStatus = data["status"]

    if ticket is None:
        ticket = SupportTicket(
            user_id=user.id,
            subject=data["subject"],
            description=data["description"],
            category=data["category"],
            priority=data["priority"],
            status=status,
            created_by=admin.id,
        )
        ticket.created_at = created
        ticket.updated_at = created
        session.add(ticket)
        await session.flush()
    else:
        ticket.description = data["description"]
        ticket.category = data["category"]
        ticket.priority = data["priority"]
        ticket.status = status
        ticket.updated_by = admin.id
        session.add(ticket)
        await session.flush()

    if status in (TicketStatus.resolved, TicketStatus.closed):
        ticket.resolved_at = created + timedelta(hours=4)
        ticket.resolved_by = admin.id
    else:
        ticket.resolved_at = None
        ticket.resolved_by = None
    session.add(ticket)

    # Reset messages
    existing_msgs = (
        await session.execute(
            select(TicketMessage).where(TicketMessage.ticket_id == ticket.id)
        )
    ).scalars().all()
    for msg in existing_msgs:
        await session.delete(msg)
    await session.flush()

    for who, body, hours_ago in data["messages"]:
        sender_id = user.id if who == "user" else admin.id
        msg_at = datetime.now(UTC) - timedelta(hours=hours_ago)
        msg = TicketMessage(
            ticket_id=ticket.id,
            sender_id=sender_id,
            body=body,
            created_by=sender_id,
        )
        msg.created_at = msg_at
        msg.updated_at = msg_at
        session.add(msg)

    await session.flush()
    number = f"TKT-{ticket.id.hex[:8].upper()}"
    return number, f"{data['subject']} [{status.value}] → {user.email}"


async def seed_support_tickets() -> list[str]:
    lines: list[str] = []
    async with async_session_factory() as session:
        admin = await _get_or_create_admin(session)
        for data in TICKETS:
            result = await _upsert_ticket(session, admin, data)
            if result is None:
                lines.append(f"SKIP — missing user {data['user_email']}")
                logger.warning("support_ticket_seed_skipped", email=data["user_email"])
                continue
            number, summary = result
            lines.append(f"{number}  {summary}")
            logger.info("support_ticket_seeded", ticket=number, subject=data["subject"])
        await session.commit()
    return lines


async def main() -> None:
    try:
        lines = await seed_support_tickets()
    finally:
        await engine.dispose()

    print("Support ticket seed results:")
    for line in lines:
        print(f"  - {line}")
    print()
    print(f"Admin login: {SEED_ADMIN_EMAIL} / {SEED_ADMIN_PASSWORD}")
    print("List: GET /api/v1/admin/support-tickets")
    if any(line.startswith("SKIP") for line in lines):
        print("Run: uv run python -m scripts.seed_seekers")
        print("     uv run python -m scripts.seed_advisors")


if __name__ == "__main__":
    asyncio.run(main())
