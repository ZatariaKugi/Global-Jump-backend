"""In-memory cooldown tracker for endpoints that trigger outbound email.

Single-process only (same constraint as ``app.services.ws_manager``'s connection
registry) — sufficient here since the goal is just to stop accidental quota
exhaustion from retries/loops, not to enforce a strict distributed rate limit.
"""

from __future__ import annotations

import time

from app.core.exceptions import RateLimitedError

_last_sent_at: dict[str, float] = {}


def enforce_cooldown(key: str, *, cooldown_seconds: int) -> None:
    """Raise ``RateLimitedError`` if ``key`` was used within ``cooldown_seconds``."""
    now = time.monotonic()
    last = _last_sent_at.get(key)
    if last is not None and now - last < cooldown_seconds:
        raise RateLimitedError()
    _last_sent_at[key] = now
