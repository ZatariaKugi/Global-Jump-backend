"""User-facing support ticket endpoints — view and reply to your own tickets (PRD §4.6)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.exceptions import PermissionDeniedError
from app.core.file_storage import resolve_url
from app.db.session import SessionDep
from app.models.ticket_message import TicketMessageAttachment
from app.models.user import User
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.support_ticket import TicketRead
from app.schemas.ticket_message import TicketMessageRead, TicketMessageSend
from app.services import support_ticket_service, ticket_message_service

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.get("", response_model=ResponseEnvelope[list[TicketRead]])
async def list_my_tickets(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[TicketRead]]:
    stmt = support_ticket_service.list_for_user_stmt(current_user.id)
    tickets, total = await paginate(session, stmt, params)
    data = [await support_ticket_service.ticket_read(session, t) for t in tickets]
    return ResponseEnvelope[list[TicketRead]](data=data, meta=page_meta(params, total, request_id))


@router.get("/{ticket_id}", response_model=ResponseEnvelope[TicketRead])
async def get_my_ticket(
    ticket_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TicketRead]:
    ticket = await support_ticket_service.get_for_user(session, ticket_id, current_user.id)
    return ResponseEnvelope[TicketRead](
        data=await support_ticket_service.ticket_read(session, ticket),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/{ticket_id}/messages",
    response_model=ResponseEnvelope[list[TicketMessageRead]],
)
async def list_my_ticket_messages(
    ticket_id: uuid.UUID,
    params: PaginationDep,
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[TicketMessageRead]]:
    ticket = await support_ticket_service.get_for_user(session, ticket_id, current_user.id)
    stmt = ticket_message_service.list_messages_stmt(ticket.id)
    messages, total = await paginate(session, stmt, params)
    senders = {m.sender_id: await session.get(User, m.sender_id) for m in messages}
    data = [
        ticket_message_service.build_message_read(m, senders.get(m.sender_id), settings)
        for m in messages
    ]
    return ResponseEnvelope[list[TicketMessageRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.post(
    "/{ticket_id}/messages",
    status_code=201,
    response_model=ResponseEnvelope[TicketMessageRead],
)
async def send_my_ticket_message(
    ticket_id: uuid.UUID,
    body: TicketMessageSend,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TicketMessageRead]:
    ticket = await support_ticket_service.get_for_user(session, ticket_id, current_user.id)

    attachments: list[TicketMessageAttachment] = []
    expected_prefix = f"ticket_attachment/{current_user.id}/"
    for ref in body.attachments:
        if not ref.file_key.startswith(expected_prefix):
            raise PermissionDeniedError("Invalid attachment key")
        attachments.append(
            TicketMessageAttachment(
                file_url=resolve_url(f"/uploads/{ref.file_key}", settings),
                file_name=ref.file_name,
                file_size=ref.file_size_bytes,
                content_type=ref.content_type,
            )
        )

    message = await ticket_message_service.send_message(
        session, ticket, current_user, body.body, attachments
    )
    return ResponseEnvelope[TicketMessageRead](
        data=ticket_message_service.build_message_read(message, current_user, settings),
        meta=Meta(request_id=request_id),
    )
