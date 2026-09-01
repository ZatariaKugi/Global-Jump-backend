"""Support / contact-us messages — save to DB and notify admin via email."""

from __future__ import annotations

from fastapi_mail import FastMail
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.support_message import SupportMessage
from app.schemas.support_message import SupportMessageCreate, SupportMessageRead
from app.services import email_service

logger = get_logger(__name__)

SUPPORT_RECIPIENT = "connect@globaljump.com"


async def create(
    session: AsyncSession,
    data: SupportMessageCreate,
    settings: Settings,
) -> SupportMessageRead:
    """Persist the support message and fire off a notification email."""
    msg = SupportMessage(
        name=data.name,
        email=data.email,
        message=data.message,
    )
    session.add(msg)
    await session.flush()
    await session.refresh(msg)

    email_service.schedule_email(
        _send_support_notification(
            sender_name=data.name,
            sender_email=data.email,
            message=data.message,
            settings=settings,
        )
    )

    return SupportMessageRead(
        id=msg.id,
        name=msg.name,
        email=msg.email,
        message=msg.message,
        created_at=msg.created_at,
    )


async def _send_support_notification(
    *,
    sender_name: str,
    sender_email: str,
    message: str,
    settings: Settings,
) -> None:
    subject = f"New support message from {sender_name}"
    ctx: dict[str, object] = {
        "sender_name": sender_name,
        "sender_email": sender_email,
        "message": message,
    }
    html = email_service._render("support_message.html", ctx, settings)
    text = email_service._render("support_message.txt", ctx, settings)

    email_msg = email_service._build_message(
        subject=subject,
        recipients=[SUPPORT_RECIPIENT],
        body=text,
        html=html,
        headers=email_service._deliverability_headers(settings, reply_to=sender_email),
    )
    if email_msg is None:
        logger.warning("support_email_skipped_invalid_recipient", recipient=SUPPORT_RECIPIENT)
        return

    fm = FastMail(email_service._make_connection(settings))
    await fm.send_message(email_msg)
