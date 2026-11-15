"""FastAPI application entrypoint for Sentinel-Gateway."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis, from_url

from app.api.routes import router
from app.core.config import Settings, settings
from app.core.limiter import MetricsStore, RateLimiter
from app.middleware.rate_limit import RateLimitMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def create_app(app_settings: Settings | None = None, redis_client: Redis | None = None) -> FastAPI:
    """Application factory supporting custom settings and injected Redis clients for testing."""
    active_settings = app_settings or settings

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        owns_redis = redis_client is None
        client = redis_client or from_url(
            active_settings.REDIS_URL,
            socket_timeout=active_settings.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=active_settings.REDIS_SOCKET_TIMEOUT,
            decode_responses=True,
        )
        metrics = MetricsStore()
        limiter = RateLimiter(redis=client, app_settings=active_settings, metrics=metrics)

        application.state.settings = active_settings
        application.state.redis = client
        application.state.metrics = metrics
        application.state.limiter = limiter

        yield

        if owns_redis:
            await client.aclose()

    application = FastAPI(
        title=active_settings.APP_NAME,
        version=active_settings.APP_VERSION,
        description=(
            "Distributed Rate Limiter & API Guard microservice powered by FastAPI and "
            "atomic Redis Lua scripts (Token Bucket & Sliding Window)."
        ),
        lifespan=lifespan,
    )

    application.add_middleware(RateLimitMiddleware, app_settings=active_settings)
    application.include_router(router)

    return application


app = create_app()
