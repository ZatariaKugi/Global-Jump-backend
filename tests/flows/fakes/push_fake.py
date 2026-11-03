"""Push without Firebase: every multicast send is recorded and reported as delivered."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from firebase_admin import messaging

from app.services import push_service


class FakePush:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send_each_for_multicast(self, message: Any, **kw: Any) -> Any:
        self.sent.append(message)
        n = len(getattr(message, "tokens", []) or [])
        return SimpleNamespace(
            success_count=n,
            failure_count=0,
            responses=[
                SimpleNamespace(success=True, exception=None, message_id=f"m{i}") for i in range(n)
            ],
        )

    def install(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(messaging, "send_each_for_multicast", self.send_each_for_multicast)
        monkeypatch.setattr(push_service, "init_firebase", lambda settings: True)
