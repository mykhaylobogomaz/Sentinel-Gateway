"""Observability and protected demo endpoints."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.limiter import MetricsStore

router = APIRouter()


class AnalyzeRequest(BaseModel):
    """Sample payload for the compute-intensive protected endpoint."""

    payload: str = Field(..., min_length=1, max_length=2000, examples=["GET /admin HTTP/1.1"])
    inspect_headers: bool = Field(default=True)


@router.get("/health", tags=["Observability"])
async def health_check(request: Request, response: Response) -> dict[str, Any]:
    """Check gateway and Redis backend health."""
    redis: Redis = request.app.state.redis
    app_settings: Settings = request.app.state.settings

    try:
        await redis.ping()
        return {
            "status": "healthy",
            "service": app_settings.APP_NAME,
            "version": app_settings.APP_VERSION,
            "redis": "connected",
            "fail_open": app_settings.FAIL_OPEN,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    except (RedisError, TimeoutError, OSError) as exc:
        if app_settings.FAIL_OPEN:
            return {
                "status": "degraded",
                "service": app_settings.APP_NAME,
                "version": app_settings.APP_VERSION,
                "redis": f"disconnected ({exc.__class__.__name__})",
                "fail_open": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        response.status_code = 503
        return {
            "status": "unhealthy",
            "service": app_settings.APP_NAME,
            "redis": f"disconnected ({exc.__class__.__name__})",
            "fail_open": False,
        }


@router.get("/metrics", tags=["Observability"])
async def get_metrics(request: Request) -> dict[str, Any]:
    """Expose gateway traffic and rate-limiting telemetry."""
    metrics: MetricsStore = request.app.state.metrics
    return metrics.snapshot()


@router.get("/api/v1/resource", tags=["Protected API"])
async def get_protected_resource(request: Request) -> dict[str, Any]:
    """Standard protected endpoint demonstrating tiered rate limiting."""
    return {
        "message": "Access granted to protected upstream resource.",
        "client_id": getattr(request.state, "client_id", "unknown"),
        "tier": getattr(request.state, "client_tier", "free"),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.post("/api/v1/analyze", tags=["Protected API"])
async def analyze_payload(body: AnalyzeRequest, request: Request) -> dict[str, Any]:
    """Compute-intensive endpoint protected by stricter burst limits on the free tier."""
    suspicious_tokens = ("../", "<script>", "UNION SELECT", "DROP TABLE", "/etc/passwd")
    matched = [token for token in suspicious_tokens if token.lower() in body.payload.lower()]

    return {
        "status": "analyzed",
        "risk_score": min(100, len(matched) * 45),
        "matched_signatures": matched,
        "client_id": getattr(request.state, "client_id", "unknown"),
        "tier": getattr(request.state, "client_tier", "free"),
    }
