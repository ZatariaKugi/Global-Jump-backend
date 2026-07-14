from __future__ import annotations

from httpx import AsyncClient


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert "X-Request-ID" in resp.headers


async def test_readiness(client: AsyncClient) -> None:
    resp = await client.get("/readiness")
    assert resp.status_code == 200
    assert resp.json()["database"] == "up"


async def test_metrics_exposed(client: AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "http_request" in resp.text
