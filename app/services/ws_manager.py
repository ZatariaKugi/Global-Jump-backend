"""In-memory WebSocket registry for real-time message delivery (PRD §3.7).

Single-process only — fine for this service's current deployment. A multi-instance
deployment would back this with a pub/sub broker (e.g. Redis) instead.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Tracks live WebSocket connections per conversation for broadcast delivery
    and presence ("is this user's socket open on this conversation")."""

    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, set[WebSocket]] = defaultdict(set)
        self._users: dict[WebSocket, uuid.UUID] = {}

    def register(
        self, conversation_id: uuid.UUID, websocket: WebSocket, user_id: uuid.UUID
    ) -> None:
        self._connections[conversation_id].add(websocket)
        self._users[websocket] = user_id

    def unregister(self, conversation_id: uuid.UUID, websocket: WebSocket) -> None:
        connections = self._connections.get(conversation_id)
        if connections is not None:
            connections.discard(websocket)
            if not connections:
                del self._connections[conversation_id]
        self._users.pop(websocket, None)

    def is_online(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return any(
            self._users.get(ws) == user_id for ws in self._connections.get(conversation_id, ())
        )

    async def broadcast(
        self,
        conversation_id: uuid.UUID,
        payload: dict[str, Any],
        *,
        exclude: WebSocket | None = None,
    ) -> None:
        for websocket in list(self._connections.get(conversation_id, ())):
            if websocket is exclude:
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                self.unregister(conversation_id, websocket)


manager = ConnectionManager()
