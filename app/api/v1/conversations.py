"""In-platform messaging endpoints (PRD §3.7)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.config import Settings
from app.core.exceptions import AppError, PermissionDeniedError
from app.core.file_storage import storage_path_from_key
from app.core.security import decode_token
from app.db.session import SessionDep
from app.models.message import Message, MessageAttachment
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreate,
    ConversationRead,
    MessageEdit,
    MessageRead,
    MessageReport,
    MessageSend,
)
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.token import TokenPayload
from app.services import conversation_service, user_service
from app.services.ws_manager import manager

router = APIRouter(tags=["conversations"])


async def _resolve_senders(session: AsyncSession, messages: list[Message]) -> dict[uuid.UUID, User]:
    senders: dict[uuid.UUID, User] = {}
    for sender_id in {m.sender_id for m in messages}:
        user = await session.get(User, sender_id)
        if user is not None:
            senders[sender_id] = user
    return senders


@router.post("/conversations", status_code=201, response_model=ResponseEnvelope[ConversationRead])
async def create_conversation(
    data: ConversationCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ConversationRead]:
    conversation = await conversation_service.get_or_create(
        session, current_user, data.other_user_id
    )
    return ResponseEnvelope[ConversationRead](
        data=await conversation_service.build_conversation_read(
            session, conversation, current_user.id
        ),
        meta=Meta(request_id=request_id),
    )


@router.get("/conversations", response_model=ResponseEnvelope[list[ConversationRead]])
async def list_conversations(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    q: str | None = None,
) -> ResponseEnvelope[list[ConversationRead]]:
    stmt = conversation_service.list_for_user_stmt(current_user.id, q)
    conversations, total = await paginate(session, stmt, params)
    data = [
        await conversation_service.build_conversation_read(session, c, current_user.id)
        for c in conversations
    ]
    return ResponseEnvelope[list[ConversationRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ResponseEnvelope[list[MessageRead]],
)
async def list_messages(
    conversation_id: uuid.UUID,
    params: PaginationDep,
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[MessageRead]]:
    conversation = await conversation_service.get_for_user(
        session, conversation_id, current_user.id
    )
    stmt = conversation_service.list_messages_stmt(conversation.id)
    messages, total = await paginate(session, stmt, params)
    senders = await _resolve_senders(session, messages)
    data = [
        conversation_service.build_message_read(m, senders.get(m.sender_id), settings)
        for m in messages
    ]
    return ResponseEnvelope[list[MessageRead]](data=data, meta=page_meta(params, total, request_id))


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=201,
    response_model=ResponseEnvelope[MessageRead],
)
async def send_message(
    conversation_id: uuid.UUID,
    data: MessageSend,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[MessageRead]:
    conversation = await conversation_service.get_for_user(
        session, conversation_id, current_user.id
    )

    attachments: list[MessageAttachment] = []
    expected_prefix = f"message_attachment/{current_user.id}/"
    for ref in data.attachments:
        if not ref.file_key.startswith(expected_prefix):
            raise PermissionDeniedError("Invalid attachment key")
        # Persist the short storage path — never a long S3 presigned URL
        # (those exceed message_attachments.file_url length and expire).
        attachments.append(
            MessageAttachment(
                file_url=storage_path_from_key(ref.file_key),
                file_name=ref.file_name,
                file_size=ref.file_size_bytes,
                content_type=ref.content_type,
            )
        )

    message = await conversation_service.send_message(
        session, conversation, current_user, data.body, attachments
    )
    message_read = conversation_service.build_message_read(message, current_user, settings)

    await manager.broadcast(
        conversation_id, {"type": "message", "data": jsonable_encoder(message_read)}
    )

    return ResponseEnvelope[MessageRead](data=message_read, meta=Meta(request_id=request_id))


@router.patch("/messages/{message_id}/read", response_model=ResponseEnvelope[MessageRead])
async def mark_message_read(
    message_id: uuid.UUID,
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[MessageRead]:
    message = await conversation_service.get_message_for_user(session, message_id, current_user.id)
    message = await conversation_service.mark_read(session, message, current_user)
    sender = await session.get(User, message.sender_id)
    data = conversation_service.build_message_read(message, sender, settings)

    await manager.broadcast(
        message.conversation_id,
        {
            "type": "read",
            "message_id": str(message.id),
            "read_at": data.read_at.isoformat() if data.read_at else None,
        },
    )

    return ResponseEnvelope[MessageRead](data=data, meta=Meta(request_id=request_id))


@router.patch("/messages/{message_id}", response_model=ResponseEnvelope[MessageRead])
async def edit_message(
    message_id: uuid.UUID,
    data: MessageEdit,
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[MessageRead]:
    message = await conversation_service.get_message_for_user(session, message_id, current_user.id)
    message = await conversation_service.edit_message(session, message, current_user.id, data.body)
    message_read = conversation_service.build_message_read(message, current_user, settings)

    await manager.broadcast(
        message.conversation_id,
        {"type": "message_edited", "data": jsonable_encoder(message_read)},
    )

    return ResponseEnvelope[MessageRead](data=message_read, meta=Meta(request_id=request_id))


@router.delete("/messages/{message_id}", status_code=204)
async def delete_message(
    message_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    message = await conversation_service.get_message_for_user(session, message_id, current_user.id)
    message = await conversation_service.delete_message(session, message, current_user.id)

    await manager.broadcast(
        message.conversation_id,
        {"type": "message_deleted", "message_id": str(message.id)},
    )


@router.post("/messages/{message_id}/report", response_model=ResponseEnvelope[MessageRead])
async def report_message(
    message_id: uuid.UUID,
    data: MessageReport,
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[MessageRead]:
    message = await conversation_service.get_message_for_user(session, message_id, current_user.id)
    message = await conversation_service.report(session, message, current_user.id, data.reason)
    sender = await session.get(User, message.sender_id)
    return ResponseEnvelope[MessageRead](
        data=conversation_service.build_message_read(message, sender, settings),
        meta=Meta(request_id=request_id),
    )


async def _authenticate_websocket(
    websocket: WebSocket, session: AsyncSession, settings: Settings
) -> User | None:
    """Resolve the connecting user from the ``Authorization`` header (or ``token`` query param).

    Performed manually rather than via ``CurrentUser`` because dependency
    exceptions raised before ``websocket.accept()`` cannot be turned into a
    clean ``websocket.close()`` from inside the route.
    """
    auth_header = websocket.headers.get("authorization")
    token: str | None = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        return None

    try:
        claims = decode_token(token, settings)
        payload = TokenPayload.model_validate(claims)
    except Exception:
        return None

    if payload.iss != settings.JWT_ISSUER:
        return None

    user = await user_service.get_by_id(session, payload.sub)
    if user is None or not user.is_active:
        return None
    return user


@router.websocket("/conversations/{conversation_id}/ws")
async def conversation_websocket(
    websocket: WebSocket,
    conversation_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    await websocket.accept()

    user = await _authenticate_websocket(websocket, session, settings)
    if user is None:
        await websocket.close(code=4401)
        return

    try:
        await conversation_service.get_for_user(session, conversation_id, user.id)
    except Exception:
        await websocket.close(code=4404)
        return

    manager.register(conversation_id, websocket, user.id)
    try:
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") != "read":
                continue
            try:
                message_id = uuid.UUID(str(payload.get("message_id")))
                message = await conversation_service.get_message_for_user(
                    session, message_id, user.id
                )
                message = await conversation_service.mark_read(session, message, user)
            except (AppError, ValueError, TypeError):
                continue
            await session.commit()
            await manager.broadcast(
                conversation_id,
                {
                    "type": "read",
                    "message_id": str(message.id),
                    "read_at": message.read_at.isoformat() if message.read_at else None,
                },
                exclude=websocket,
            )
    except WebSocketDisconnect:
        pass
    finally:
        manager.unregister(conversation_id, websocket)
