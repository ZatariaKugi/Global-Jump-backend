"""Shared AsyncOpenAI client factory — avoids per-request TCP connection pools."""

from openai import AsyncOpenAI

from app.core.config import Settings

# Module-level cache: one client per (api_key, timeout) combination.
# AsyncOpenAI is safe to share across async tasks; it uses httpx.AsyncClient
# internally which handles concurrent requests correctly.
_clients: dict[tuple[str, float], AsyncOpenAI] = {}


def get_openai_client(
    settings: Settings, *, timeout: float | None = None
) -> AsyncOpenAI | None:
    """Return a cached ``AsyncOpenAI`` client, or ``None`` when unconfigured.

    The client is reused across the application lifetime so that TCP connections
    to the OpenAI API are pooled rather than re-created on every call.

    Pass ``timeout`` to override the default ``OPENAI_TIMEOUT_SECONDS`` (used by
    the country-rule web-search service which needs a longer window).
    """
    if not settings.OPENAI_API_KEY:
        return None

    effective_timeout = timeout if timeout is not None else settings.OPENAI_TIMEOUT_SECONDS
    key = (settings.OPENAI_API_KEY, effective_timeout)
    if key in _clients:
        return _clients[key]

    client = AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=effective_timeout,
        max_retries=1,
    )
    _clients[key] = client
    return client


async def close_clients() -> None:
    """Close pooled OpenAI clients on application shutdown."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        await client.close()
