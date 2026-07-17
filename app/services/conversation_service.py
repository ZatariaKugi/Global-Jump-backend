"""In-platform messaging — conversations, message thread, read receipts, moderation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from app.core.file_storage import resolve_url
from app.models.conversation import Conversation
from app.models.message import Message, MessageAttachment
from app.models.review import ModerationStatus
from app.models.user import User, UserRole
from app.schemas.conversation import (
    AttachmentRead,
    ConversationRead,
    FlaggedMessageRead,
    MessageRead,
)
from app.services.ws_manager import manager

PUBLIC_STATUSES = (ModerationStatus.visible, ModerationStatus.flagged)


async def conversation_ids_for_seeker(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    advisor_ids: list[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """Map advisor_id → conversation.id for threads the seeker already has."""
    if not advisor_ids:
        return {}
    rows = (
        await session.execute(
            select(Conversation.advisor_id, Conversation.id)
            .where(Conversation.seeker_id == seeker_id)
            .where(Conversation.advisor_id.in_(advisor_ids))
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def get_or_create(
    session: AsyncSession, current_user: User, other_user_id: uuid.UUID
) -> Conversation:
    """Open (or return) a seeker↔advisor thread. Booking is not required."""
    if other_user_id == current_user.id:
        raise AppError("Cannot start a conversation with yourself", code="invalid_participants")

    other = await session.get(User, other_user_id)
    if other is None:
        raise NotFoundError("User not found")

    if current_user.role == UserRole.seeker and other.role == UserRole.advisor:
        seeker_id, advisor_id = current_user.id, other.id
    elif current_user.role == UserRole.advisor and other.role == UserRole.seeker:
        seeker_id, advisor_id = other.id, current_user.id
    else:
        raise AppError(
            "Conversations are only allowed between a seeker and an advisor",
            code="invalid_participants",
        )

    existing = await session.execute(
        select(Conversation)
        .where(Conversation.seeker_id == seeker_id)
        .where(Conversation.advisor_id == advisor_id)
    )
    conversation = existing.scalar_one_or_none()
    if conversation is not None:
        return conversation

    conversation = Conversation(
        seeker_id=seeker_id, advisor_id=advisor_id, created_by=current_user.id
    )
    session.add(conversation)
    await session.flush()
    await session.refresh(conversation)
    return conversation


def list_for_user_stmt(user_id: uuid.UUID, q: str | None = None) -> Select[tuple[Conversation]]:
    stmt = select(Conversation).where(
        or_(Conversation.seeker_id == user_id, Conversation.advisor_id == user_id)
    )
    if q:
        other_party_id_expr = case(
            (Conversation.seeker_id == user_id, Conversation.advisor_id),
            else_=Conversation.seeker_id,
        )
        stmt = stmt.join(User, User.id == other_party_id_expr).where(
            User.full_name.ilike(f"%{q.strip()}%")
        )
    return stmt.order_by(
        Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc()
    )


async def get_for_user(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or user_id not in (conversation.seeker_id, conversation.advisor_id):
        raise NotFoundError("Conversation not found")
    return conversation


def other_party_id(conversation: Conversation, user_id: uuid.UUID) -> uuid.UUID:
    if user_id == conversation.seeker_id:
        return conversation.advisor_id
    return conversation.seeker_id


def list_messages_stmt(conversation_id: uuid.UUID) -> Select[tuple[Message]]:
    return (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .where(Message.moderation_status.in_(PUBLIC_STATUSES))
        .where(Message.deleted_at.is_(None))
        .order_by(Message.created_at.asc())
    )


async def send_message(
    session: AsyncSession,
    conversation: Conversation,
    sender: User,
    body: str | None,
    attachments: list[MessageAttachment] | None = None,
) -> Message:
    body = body.strip() if body else None
    attachments = attachments or []
    if not body and not attachments:
        raise AppError("Message must contain text or an attachment", code="empty_message")

    now = datetime.now(UTC)
    message = Message(
        conversation_id=conversation.id,
        sender_id=sender.id,
        body=body,
        created_by=sender.id,
        created_at=now,  # explicit (sub-second) timestamp so message order is unambiguous
    )
    message.attachments = attachments
    session.add(message)

    conversation.last_message_at = now
    conversation.updated_by = sender.id
    session.add(conversation)

    await session.flush()
    await session.refresh(message)
    return message


async def get_message_for_user(
    session: AsyncSession, message_id: uuid.UUID, user_id: uuid.UUID
) -> Message:
    message = await session.get(Message, message_id)
    if message is None:
        raise NotFoundError("Message not found")
    conversation = await session.get(Conversation, message.conversation_id)
    if conversation is None or user_id not in (conversation.seeker_id, conversation.advisor_id):
        raise NotFoundError("Message not found")
    return message


async def mark_read(session: AsyncSession, message: Message, user: User) -> Message:
    if message.sender_id == user.id:
        raise AppError("Cannot mark your own message as read", code="invalid_state")
    if message.read_at is None:
        message.read_at = datetime.now(UTC)
        message.updated_by = user.id
        session.add(message)
        await session.flush()
        await session.refresh(message)
    return message


async def edit_message(
    session: AsyncSession, message: Message, editor_id: uuid.UUID, new_body: str
) -> Message:
    if message.sender_id != editor_id:
        raise PermissionDeniedError("Only the sender can edit this message")
    if message.deleted_at is not None or message.moderation_status == ModerationStatus.removed:
        raise AppError("Cannot edit a deleted message", code="invalid_state")

    new_body = new_body.strip()
    if not new_body:
        raise AppError("Message body cannot be empty", code="empty_message")

    message.body = new_body
    message.edited_at = datetime.now(UTC)
    message.updated_by = editor_id
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


async def delete_message(session: AsyncSession, message: Message, actor_id: uuid.UUID) -> Message:
    if message.sender_id != actor_id:
        raise PermissionDeniedError("Only the sender can delete this message")
    if message.deleted_at is not None:
        raise AppError("Message already deleted", code="invalid_state")

    message.deleted_at = datetime.now(UTC)
    message.deleted_by = actor_id
    message.updated_by = actor_id
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


async def unread_count(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    result = await session.execute(
        select(func.count(Message.id))
        .where(Message.conversation_id == conversation_id)
        .where(Message.sender_id != user_id)
        .where(Message.read_at.is_(None))
        .where(Message.moderation_status.in_(PUBLIC_STATUSES))
    )
    return int(result.scalar_one())


async def last_message(session: AsyncSession, conversation_id: uuid.UUID) -> Message | None:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .where(Message.moderation_status.in_(PUBLIC_STATUSES))
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def report(
    session: AsyncSession, message: Message, reporter_id: uuid.UUID, reason: str
) -> Message:
    if message.moderation_status == ModerationStatus.removed:
        raise AppError("Message already removed", code="invalid_state")
    message.moderation_status = ModerationStatus.flagged
    message.flag_reason = reason
    message.flagged_by = reporter_id
    message.updated_by = reporter_id
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


def list_flagged_stmt() -> Select[tuple[Message]]:
    return (
        select(Message)
        .where(Message.moderation_status == ModerationStatus.flagged)
        .order_by(Message.updated_at.desc())
    )


async def get_by_id(session: AsyncSession, message_id: uuid.UUID) -> Message:
    message = await session.get(Message, message_id)
    if message is None:
        raise NotFoundError("Message not found")
    return message


async def moderate(
    session: AsyncSession, message: Message, action: str, admin_id: uuid.UUID
) -> Message:
    if action == "approve":
        message.moderation_status = ModerationStatus.visible
        message.flag_reason = None
        message.flagged_by = None
    else:  # remove
        message.moderation_status = ModerationStatus.removed
    message.updated_by = admin_id
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


def build_message_read(message: Message, sender: User | None, settings: Settings) -> MessageRead:
    return MessageRead(
        id=message.id,
        conversation_id=message.conversation_id,
        sender_id=message.sender_id,
        sender_name=sender.full_name if sender else None,
        body=message.body,
        attachments=[
            AttachmentRead(
                id=a.id,
                file_url=resolve_url(a.file_url, settings),
                file_name=a.file_name,
                file_size=a.file_size,
                content_type=a.content_type,
            )
            for a in message.attachments
        ],
        read_at=message.read_at,
        edited_at=message.edited_at,
        created_at=message.created_at,
    )


async def build_conversation_read(
    session: AsyncSession, conversation: Conversation, current_user_id: uuid.UUID
) -> ConversationRead:
    other_id = other_party_id(conversation, current_user_id)
    other = await session.get(User, other_id)
    last = await last_message(session, conversation.id)
    unread = await unread_count(session, conversation.id, current_user_id)
    return ConversationRead(
        id=conversation.id,
        seeker_id=conversation.seeker_id,
        advisor_id=conversation.advisor_id,
        other_party_id=other_id,
        other_party_name=other.full_name if other else None,
        other_party_online=manager.is_online(conversation.id, other_id),
        last_message_at=conversation.last_message_at,
        last_message_preview=last.body[:140] if last and last.body else None,
        unread_count=unread,
        created_at=conversation.created_at,
    )


def build_flagged_read(
    message: Message, sender: User | None, settings: Settings
) -> FlaggedMessageRead:
    base = build_message_read(message, sender, settings)
    return FlaggedMessageRead(
        **base.model_dump(),
        moderation_status=message.moderation_status,
        flag_reason=message.flag_reason,
        flagged_by=message.flagged_by,
    )
