"""In-platform messaging tests (epic #9, PRD §3.7)."""

from __future__ import annotations

import io
import uuid

from httpx import AsyncClient

from app.services.ws_manager import ConnectionManager
from tests.test_bookings import _bookable_advisor, _seeker, _slot_iso

CONVERSATIONS = "/api/v1/conversations"


async def _user_id(client: AsyncClient, headers: dict) -> str:
    resp = await client.get("/api/v1/users/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["data"]["id"])


async def _booked_pair(client: AsyncClient, engine) -> tuple[str, dict, str, dict]:
    """Returns (advisor_id, advisor_headers, seeker_id, seeker_headers).

    The pair already has a booking, satisfying the conversation prerequisite.
    """
    advisor_id, advisor_headers, day = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)
    resp = await client.post(
        "/api/v1/bookings",
        json={
            "advisor_id": advisor_id,
            "service_type": "consultation_30",
            "scheduled_start": _slot_iso(day, 10),
        },
        headers=cust_headers,
    )
    assert resp.status_code == 201, resp.text
    seeker_id = await _user_id(client, cust_headers)
    return advisor_id, advisor_headers, seeker_id, cust_headers


# ── Conversation creation ────────────────────────────────────────────────────


async def test_create_conversation_requires_booking_relationship(
    client: AsyncClient, engine
) -> None:
    advisor_id, _, _ = await _bookable_advisor(client, engine)
    _, cust_headers = await _seeker(client)  # no booking made

    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_create_conversation_happy_path_and_idempotent(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, seeker_id, cust_headers = await _booked_pair(client, engine)

    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    conversation_id = data["id"]
    assert data["seeker_id"] == seeker_id
    assert data["advisor_id"] == advisor_id
    assert data["other_party_id"] == advisor_id
    assert data["other_party_name"] == "Bookable Advisor"
    assert data["unread_count"] == 0
    assert data["last_message_at"] is None

    # Idempotent — calling again returns the same conversation.
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["id"] == conversation_id

    # Advisor side resolves to the same conversation.
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": seeker_id}, headers=advisor_headers
    )
    assert resp.status_code == 201
    advisor_data = resp.json()["data"]
    assert advisor_data["id"] == conversation_id
    assert advisor_data["other_party_id"] == seeker_id


async def test_create_conversation_rejects_invalid_participants(
    client: AsyncClient, engine
) -> None:
    _, _, seeker_id, cust_headers = await _booked_pair(client, engine)
    _, cust2_headers = await _seeker(client, "cust2@test.com")

    # Cannot start a conversation with yourself.
    resp = await client.post(CONVERSATIONS, json={"other_user_id": seeker_id}, headers=cust_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_participants"

    # Two seekers cannot converse with each other.
    cust2_id = await _user_id(client, cust2_headers)
    resp = await client.post(CONVERSATIONS, json={"other_user_id": cust2_id}, headers=cust_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_participants"

    # Unknown user.
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": str(uuid.uuid4())}, headers=cust_headers
    )
    assert resp.status_code == 404


# ── Messages ──────────────────────────────────────────────────────────────


async def test_send_and_list_messages_with_unread_and_preview(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, seeker_id, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hello, I need help with my visa."},
        headers=cust_headers,
    )
    assert resp.status_code == 201, resp.text
    msg1 = resp.json()["data"]
    assert msg1["body"] == "Hello, I need help with my visa."
    assert msg1["sender_id"] == seeker_id
    assert msg1["sender_name"] == "Seeker"
    assert msg1["attachments"] == []
    assert msg1["read_at"] is None

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Sure, happy to help!"},
        headers=advisor_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["sender_name"] == "Bookable Advisor"

    resp = await client.get(f"{CONVERSATIONS}/{conversation_id}/messages", headers=cust_headers)
    assert resp.status_code == 200
    bodies = [m["body"] for m in resp.json()["data"]]
    assert bodies == ["Hello, I need help with my visa.", "Sure, happy to help!"]

    resp = await client.get(CONVERSATIONS, headers=cust_headers)
    conv = resp.json()["data"][0]
    assert conv["last_message_preview"] == "Sure, happy to help!"
    assert conv["last_message_at"] is not None
    assert conv["unread_count"] == 1  # advisor's reply is unread by the seeker


async def test_send_message_requires_body_or_attachment(client: AsyncClient, engine) -> None:
    advisor_id, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "   "},
        headers=cust_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_message"


async def test_send_message_with_attachment(client: AsyncClient, engine) -> None:
    advisor_id, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    # Upload file via global endpoint first
    upload = await client.post(
        "/api/v1/uploads",
        headers=cust_headers,
        files={
            "file": ("passport.pdf", io.BytesIO(b"%PDF-1.4 fake pdf content"), "application/pdf")
        },
        data={"category": "message_attachment"},
    )
    assert upload.status_code == 201, upload.text
    file_info = upload.json()["data"]

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={
            "body": "See the attached document.",
            "attachments": [
                {
                    "file_key": file_info["file_key"],
                    "file_name": "passport.pdf",
                    "file_size_bytes": file_info["file_size_bytes"],
                    "content_type": "application/pdf",
                }
            ],
        },
        headers=cust_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert len(data["attachments"]) == 1
    attachment = data["attachments"][0]
    assert attachment["file_name"] == "passport.pdf"
    assert attachment["content_type"] == "application/pdf"
    assert attachment["file_size"] > 0


async def test_send_attachment_only_message(client: AsyncClient, engine) -> None:
    advisor_id, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    # Upload file via global endpoint first
    upload = await client.post(
        "/api/v1/uploads",
        headers=cust_headers,
        files={"file": ("photo.jpg", io.BytesIO(b"fake jpg bytes"), "image/jpeg")},
        data={"category": "message_attachment"},
    )
    assert upload.status_code == 201, upload.text
    file_info = upload.json()["data"]

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={
            "attachments": [
                {
                    "file_key": file_info["file_key"],
                    "file_name": "photo.jpg",
                    "file_size_bytes": file_info["file_size_bytes"],
                    "content_type": "image/jpeg",
                }
            ]
        },
        headers=cust_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["body"] is None
    assert len(data["attachments"]) == 1
    assert data["attachments"][0]["file_name"] == "photo.jpg"


# ── Read receipts ────────────────────────────────────────────────────────────


async def test_mark_message_read(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hello"},
        headers=cust_headers,
    )
    message_id = resp.json()["data"]["id"]

    # Sender cannot mark their own message as read.
    resp = await client.patch(f"/api/v1/messages/{message_id}/read", headers=cust_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"

    # Recipient marks it read.
    resp = await client.patch(f"/api/v1/messages/{message_id}/read", headers=advisor_headers)
    assert resp.status_code == 200
    read_at = resp.json()["data"]["read_at"]
    assert read_at is not None

    # Idempotent.
    resp = await client.patch(f"/api/v1/messages/{message_id}/read", headers=advisor_headers)
    assert resp.json()["data"]["read_at"] == read_at

    # Conversation unread count drops to zero for the advisor.
    resp = await client.get(CONVERSATIONS, headers=advisor_headers)
    conv = resp.json()["data"][0]
    assert conv["unread_count"] == 0


# ── Message edit / delete ────────────────────────────────────────────────────


async def test_sender_can_edit_own_message(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hello"},
        headers=cust_headers,
    )
    message_id = resp.json()["data"]["id"]
    assert resp.json()["data"]["edited_at"] is None

    resp = await client.patch(
        f"/api/v1/messages/{message_id}", json={"body": "Hello there!"}, headers=cust_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["body"] == "Hello there!"
    assert data["edited_at"] is not None


async def test_non_sender_cannot_edit_message(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]
    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hello"},
        headers=cust_headers,
    )
    message_id = resp.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/messages/{message_id}", json={"body": "Hijacked!"}, headers=advisor_headers
    )
    assert resp.status_code == 403


async def test_edit_rejects_empty_body(client: AsyncClient, engine) -> None:
    advisor_id, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]
    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hello"},
        headers=cust_headers,
    )
    message_id = resp.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/messages/{message_id}", json={"body": "   "}, headers=cust_headers
    )
    # Whitespace passes the schema's min_length=1 but is rejected once stripped.
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_message"


async def test_sender_can_delete_own_message(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]
    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hello"},
        headers=cust_headers,
    )
    message_id = resp.json()["data"]["id"]

    resp = await client.delete(f"/api/v1/messages/{message_id}", headers=cust_headers)
    assert resp.status_code == 204

    resp = await client.get(f"{CONVERSATIONS}/{conversation_id}/messages", headers=cust_headers)
    assert resp.json()["data"] == []

    # Deleting again is rejected.
    resp = await client.delete(f"/api/v1/messages/{message_id}", headers=cust_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


async def test_non_sender_cannot_delete_message(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]
    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hello"},
        headers=cust_headers,
    )
    message_id = resp.json()["data"]["id"]

    resp = await client.delete(f"/api/v1/messages/{message_id}", headers=advisor_headers)
    assert resp.status_code == 403


# ── Conversation search ───────────────────────────────────────────────────────


async def test_conversation_search_filters_by_other_party_name(client: AsyncClient, engine) -> None:
    advisor_id, advisor_headers, _, cust_headers = await _booked_pair(client, engine)
    await client.post(CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers)

    resp = await client.get(f"{CONVERSATIONS}?q=Bookable", headers=cust_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1

    resp = await client.get(f"{CONVERSATIONS}?q=NoSuchPerson", headers=cust_headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == []


# ── Presence ──────────────────────────────────────────────────────────────────


async def test_other_party_online_reflects_presence(client: AsyncClient, engine) -> None:
    from app.services.ws_manager import manager

    advisor_id, advisor_headers, seeker_id, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = uuid.UUID(resp.json()["data"]["id"])
    assert resp.json()["data"]["other_party_online"] is False

    fake_ws = _FakeWebSocket()
    manager.register(conversation_id, fake_ws, uuid.UUID(advisor_id))  # type: ignore[arg-type]
    try:
        resp = await client.get(CONVERSATIONS, headers=cust_headers)
        conv = next(c for c in resp.json()["data"] if c["id"] == str(conversation_id))
        assert conv["other_party_online"] is True
    finally:
        manager.unregister(conversation_id, fake_ws)  # type: ignore[arg-type]

    resp = await client.get(CONVERSATIONS, headers=cust_headers)
    conv = next(c for c in resp.json()["data"] if c["id"] == str(conversation_id))
    assert conv["other_party_online"] is False


# ── Reporting & admin moderation ─────────────────────────────────────────────


async def test_report_and_admin_moderation_flow(
    client: AsyncClient, engine, admin_token: str
) -> None:
    advisor_id, advisor_headers, _, cust_headers = await _booked_pair(client, engine)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "inappropriate content"},
        headers=cust_headers,
    )
    message_id = resp.json()["data"]["id"]

    # Advisor reports the message.
    resp = await client.post(
        f"/api/v1/messages/{message_id}/report",
        json={"reason": "Inappropriate content"},
        headers=advisor_headers,
    )
    assert resp.status_code == 200, resp.text

    # It appears in the admin queue.
    resp = await client.get("/api/v1/admin/messages/flagged", headers=admin_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 1
    flagged = resp.json()["data"][0]
    assert flagged["flag_reason"] == "Inappropriate content"
    assert flagged["moderation_status"] == "flagged"

    # While flagged it is still visible in the conversation.
    resp = await client.get(f"{CONVERSATIONS}/{conversation_id}/messages", headers=advisor_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 1

    # Admin removes it.
    resp = await client.patch(
        f"/api/v1/admin/messages/{message_id}/moderation",
        json={"action": "remove"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["moderation_status"] == "removed"

    # No longer visible in the conversation.
    resp = await client.get(f"{CONVERSATIONS}/{conversation_id}/messages", headers=advisor_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 0

    # Queue is now empty.
    resp = await client.get("/api/v1/admin/messages/flagged", headers=admin_headers)
    assert resp.json()["meta"]["pagination"]["total"] == 0

    # Reporting an already-removed message fails.
    resp = await client.post(
        f"/api/v1/messages/{message_id}/report",
        json={"reason": "again"},
        headers=advisor_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_state"


async def test_message_moderation_requires_admin(client: AsyncClient, engine) -> None:
    _, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.get("/api/v1/admin/messages/flagged", headers=cust_headers)
    assert resp.status_code == 403


# ── RBAC / not-found guards ───────────────────────────────────────────────────


async def test_stranger_cannot_access_conversation(client: AsyncClient, engine) -> None:
    advisor_id, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.post(
        CONVERSATIONS, json={"other_user_id": advisor_id}, headers=cust_headers
    )
    conversation_id = resp.json()["data"]["id"]

    _, stranger_headers = await _seeker(client, "stranger@test.com")

    resp = await client.get(f"{CONVERSATIONS}/{conversation_id}/messages", headers=stranger_headers)
    assert resp.status_code == 404

    resp = await client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages",
        json={"body": "Hi"},
        headers=stranger_headers,
    )
    assert resp.status_code == 404


async def test_get_messages_unknown_conversation(client: AsyncClient, engine) -> None:
    _, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.get(f"{CONVERSATIONS}/{uuid.uuid4()}/messages", headers=cust_headers)
    assert resp.status_code == 404


async def test_mark_read_unknown_message(client: AsyncClient, engine) -> None:
    _, _, _, cust_headers = await _booked_pair(client, engine)
    resp = await client.patch(f"/api/v1/messages/{uuid.uuid4()}/read", headers=cust_headers)
    assert resp.status_code == 404


# ── WebSocket connection manager ─────────────────────────────────────────────


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail = False

    async def send_json(self, data: dict) -> None:
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(data)


async def test_connection_manager_broadcast_and_exclude() -> None:
    manager = ConnectionManager()
    conversation_id = uuid.uuid4()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    manager.register(conversation_id, ws1, uuid.uuid4())  # type: ignore[arg-type]
    manager.register(conversation_id, ws2, uuid.uuid4())  # type: ignore[arg-type]

    await manager.broadcast(conversation_id, {"type": "message"}, exclude=ws1)  # type: ignore[arg-type]
    assert ws1.sent == []
    assert ws2.sent == [{"type": "message"}]


async def test_connection_manager_drops_dead_connections() -> None:
    manager = ConnectionManager()
    conversation_id = uuid.uuid4()
    ws = _FakeWebSocket()
    ws.fail = True
    manager.register(conversation_id, ws, uuid.uuid4())  # type: ignore[arg-type]

    await manager.broadcast(conversation_id, {"type": "message"})

    # The dead connection was dropped; re-broadcasting is now a silent no-op.
    ws.fail = False
    await manager.broadcast(conversation_id, {"type": "message"})
    assert ws.sent == []


async def test_connection_manager_unregister() -> None:
    manager = ConnectionManager()
    conversation_id = uuid.uuid4()
    ws = _FakeWebSocket()
    manager.register(conversation_id, ws, uuid.uuid4())  # type: ignore[arg-type]
    manager.unregister(conversation_id, ws)  # type: ignore[arg-type]

    await manager.broadcast(conversation_id, {"type": "message"})
    assert ws.sent == []

    # Unregistering something not (or no longer) tracked is a no-op.
    manager.unregister(conversation_id, ws)  # type: ignore[arg-type]
    manager.unregister(uuid.uuid4(), ws)  # type: ignore[arg-type]


async def test_connection_manager_is_online() -> None:
    manager = ConnectionManager()
    conversation_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ws = _FakeWebSocket()

    assert manager.is_online(conversation_id, user_id) is False

    manager.register(conversation_id, ws, user_id)  # type: ignore[arg-type]
    assert manager.is_online(conversation_id, user_id) is True
    assert manager.is_online(conversation_id, uuid.uuid4()) is False

    manager.unregister(conversation_id, ws)  # type: ignore[arg-type]
    assert manager.is_online(conversation_id, user_id) is False
