"""End-to-end HTTP middleware tests for headers, tiers, and 429 responses."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_headers_and_429_on_strict_endpoint(async_client: AsyncClient) -> None:
    """Free tier on /api/v1/analyze has burst capacity=3; 4th call returns 429 + Retry-After."""
    payload = {"payload": "SELECT * FROM users", "inspect_headers": True}

    for expected_rem in ("2", "1", "0"):
        resp = await async_client.post("/api/v1/analyze", json=payload)
        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "3"
        assert resp.headers["X-RateLimit-Remaining"] == expected_rem
        assert "X-RateLimit-Reset" in resp.headers

    blocked = await async_client.post("/api/v1/analyze", json=payload)
    assert blocked.status_code == 429
    assert blocked.headers["X-RateLimit-Remaining"] == "0"
    assert int(blocked.headers["Retry-After"]) >= 1
    body = blocked.json()
    assert body["tier"] == "free"
    assert body["algorithm"] == "token_bucket"


@pytest.mark.asyncio
async def test_api_key_tier_elevation(async_client: AsyncClient) -> None:
    """Clients with a Pro API key receive higher burst limits than anonymous free clients."""
    resp = await async_client.get(
        "/api/v1/resource",
        headers={"X-API-Key": "sg_pro_demo_key"},
    )
    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "60"
    assert resp.headers["X-RateLimit-Remaining"] == "59"
    assert "tier=pro" in resp.headers["X-RateLimit-Policy"]
    assert resp.json()["tier"] == "pro"


@pytest.mark.asyncio
async def test_sliding_window_header_selection(async_client: AsyncClient) -> None:
    """Clients can select the sliding_window algorithm via X-RateLimit-Algorithm header."""
    resp = await async_client.get(
        "/api/v1/resource",
        headers={"X-RateLimit-Algorithm": "sliding_window"},
    )
    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "20"
    assert "sliding_window" in resp.headers["X-RateLimit-Policy"]


@pytest.mark.asyncio
async def test_health_and_metrics_endpoints(async_client: AsyncClient) -> None:
    """Observability endpoints report health status and accurate request counters."""
    await async_client.get("/api/v1/resource")

    health_resp = await async_client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "healthy"
    assert health_resp.json()["redis"] == "connected"

    metrics_resp = await async_client.get("/metrics")
    assert metrics_resp.status_code == 200
    metrics = metrics_resp.json()
    assert metrics["total_requests"] >= 1
    assert metrics["allowed_requests"] >= 1
