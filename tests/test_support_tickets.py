"""Support ticket tests (PRD §4.6 Support & Moderation)."""

from __future__ import annotations

import io
import uuid

from httpx import AsyncClient

TICKETS_ADMIN = "/api/v1/admin/support-tickets"
TICKETS = "/api/v1/tickets"


async def _user(
    client: AsyncClient, email: str = "seeker@test.com", full_name: str = "Support Seeker"
) -> tuple[str, dict, str]:
    """Register + log in a user. Returns (user_id, headers, token)."""
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "custpass123", "full_name": full_name},
    )
    resp = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "custpass123"}
    )
    token = str(resp.json()["access_token"])
    headers = {"Authorization": f"Bearer {token}"}
    me = await client.get("/api/v1/users/me", headers=headers)
    return str(me.json()["data"]["id"]), headers, token


async def _admin_ticket(
    client: AsyncClient,
    admin_headers: dict,
    user_id: str,
    subject: str = "Cannot log in",
    category: str = "technical",
    priority: str = "medium",
) -> str:
    resp = await client.post(
        TICKETS_ADMIN,
        json={
            "user_id": user_id,
            "subject": subject,
            "description": "User reports an issue.",
            "category": category,
            "priority": priority,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["data"]["id"])


# ── Admin CRUD ────────────────────────────────────────────────────────────────


async def test_admin_can_create_list_detail_update_ticket(
    client: AsyncClient, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_id, _, _ = await _user(client)

    ticket_id = await _admin_ticket(client, admin_headers, user_id, priority="high")

    resp = await client.get(TICKETS_ADMIN, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["status"] == "open"
    assert data[0]["priority"] == "high"
    assert data[0]["user_name"] == "Support Seeker"

    resp = await client.get(f"{TICKETS_ADMIN}/{ticket_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["subject"] == "Cannot log in"

    resp = await client.patch(
        f"{TICKETS_ADMIN}/{ticket_id}", json={"status": "resolved"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "resolved"
    assert data["resolved_at"] is not None
    assert data["resolved_by"] is not None


async def test_admin_can_filter_tickets(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_id, _, _ = await _user(client)
    await _admin_ticket(
        client, admin_headers, user_id, subject="Billing issue", category="billing", priority="low"
    )
    await _admin_ticket(
        client,
        admin_headers,
        user_id,
        subject="Tech issue",
        category="technical",
        priority="urgent",
    )

    resp = await client.get(f"{TICKETS_ADMIN}?category=billing", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["subject"] == "Billing issue"

    resp = await client.get(f"{TICKETS_ADMIN}?priority=urgent", headers=admin_headers)
    assert len(resp.json()["data"]) == 1

    resp = await client.get(f"{TICKETS_ADMIN}?search=Billing", headers=admin_headers)
    assert len(resp.json()["data"]) == 1


async def test_ticket_requires_valid_user(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    resp = await client.post(
        TICKETS_ADMIN,
        json={
            "user_id": str(uuid.uuid4()),
            "subject": "x",
            "description": "d",
            "category": "other",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 404


async def test_non_admin_forbidden_from_ticket_admin_endpoints(
    client: AsyncClient, admin_token: str
) -> None:
    user_id, user_headers, _ = await _user(client)
    resp = await client.get(TICKETS_ADMIN, headers=user_headers)
    assert resp.status_code == 403
    resp = await client.post(
        TICKETS_ADMIN,
        json={"user_id": user_id, "subject": "x", "description": "y", "category": "other"},
        headers=user_headers,
    )
    assert resp.status_code == 403


# ── Conversation thread ───────────────────────────────────────────────────────


async def test_conversation_thread_two_way(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_id, user_headers, _ = await _user(client)
    ticket_id = await _admin_ticket(client, admin_headers, user_id)

    resp = await client.post(
        f"{TICKETS_ADMIN}/{ticket_id}/messages",
        json={"body": "Hi, how can we help?"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        f"{TICKETS}/{ticket_id}/messages",
        json={"body": "I can't reset my password"},
        headers=user_headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(f"{TICKETS_ADMIN}/{ticket_id}/messages", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    bodies = [m["body"] for m in resp.json()["data"]]
    assert bodies == ["Hi, how can we help?", "I can't reset my password"]

    resp = await client.get(f"{TICKETS}/{ticket_id}/messages", headers=user_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 2


async def test_ticket_message_requires_body_or_attachment(
    client: AsyncClient, admin_token: str
) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_id, _, _ = await _user(client)
    ticket_id = await _admin_ticket(client, admin_headers, user_id)

    resp = await client.post(
        f"{TICKETS_ADMIN}/{ticket_id}/messages", json={}, headers=admin_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_message"


async def test_ticket_message_with_attachment(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_id, user_headers, _ = await _user(client)
    ticket_id = await _admin_ticket(client, admin_headers, user_id)

    content = b"%PDF-1.4 test content"
    resp = await client.post(
        "/api/v1/uploads",
        headers=user_headers,
        files={"file": ("proof.pdf", io.BytesIO(content), "application/pdf")},
        data={"category": "ticket_attachment"},
    )
    assert resp.status_code == 201, resp.text
    upload = resp.json()["data"]

    resp = await client.post(
        f"{TICKETS}/{ticket_id}/messages",
        json={
            "attachments": [
                {
                    "file_key": upload["file_key"],
                    "file_name": "proof.pdf",
                    "file_size_bytes": upload["file_size_bytes"],
                    "content_type": "application/pdf",
                }
            ]
        },
        headers=user_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert len(data["attachments"]) == 1
    assert data["attachments"][0]["file_name"] == "proof.pdf"


# ── User-side access ──────────────────────────────────────────────────────────


async def test_user_sees_only_own_tickets(client: AsyncClient, admin_token: str) -> None:
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user_a_id, user_a_headers, _ = await _user(client, "a@test.com", "User A")
    _, user_b_headers, _ = await _user(client, "b@test.com", "User B")
    ticket_id = await _admin_ticket(client, admin_headers, user_a_id, subject="A's ticket")

    resp = await client.get(f"{TICKETS}/{ticket_id}", headers=user_a_headers)
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"{TICKETS}/{ticket_id}", headers=user_b_headers)
    assert resp.status_code == 404

    resp = await client.get(TICKETS, headers=user_a_headers)
    assert len(resp.json()["data"]) == 1
    resp = await client.get(TICKETS, headers=user_b_headers)
    assert resp.json()["data"] == []
