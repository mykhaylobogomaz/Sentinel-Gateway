"""Pytest fixtures for async Redis and FastAPI integration testing."""

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis, from_url

from app.core.config import Settings
from app.core.limiter import MetricsStore, RateLimiter
from app.main import create_app

TEST_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def test_settings() -> Settings:
    """Provide isolated test settings."""
    return Settings(
        REDIS_URL=TEST_REDIS_URL,
        FAIL_OPEN=True,
        DEFAULT_ALGORITHM="token_bucket",
    )


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Provide a clean Redis database for each test case."""
    client = from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def metrics_store() -> MetricsStore:
    """Provide a fresh MetricsStore instance."""
    return MetricsStore()


@pytest.fixture
def rate_limiter(
    redis_client: Redis,
    test_settings: Settings,
    metrics_store: MetricsStore,
) -> RateLimiter:
    """Provide a configured RateLimiter wired to the test Redis client."""
    return RateLimiter(redis=redis_client, app_settings=test_settings, metrics=metrics_store)


@pytest_asyncio.fixture
async def async_client(
    test_settings: Settings,
    redis_client: Redis,
) -> AsyncIterator[AsyncClient]:
    """Provide an HTTPX AsyncClient running the full FastAPI lifespan and middleware."""
    app = create_app(app_settings=test_settings, redis_client=redis_client)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
