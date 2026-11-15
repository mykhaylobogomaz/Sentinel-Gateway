"""FastAPI middleware enforcing distributed rate limits and injecting standard headers."""

import math
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import Settings
from app.core.limiter import AlgorithmType, RateLimiter, RateLimitResult


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Intercepts incoming requests, resolves client tier, and enforces rate limits."""

    def __init__(self, app, app_settings: Settings) -> None:
        super().__init__(app)
        self.settings = app_settings

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in self.settings.EXEMPT_PATHS:
            return await call_next(request)

        limiter: RateLimiter = request.app.state.limiter
        client_id, tier = self._identify_client(request)
        policy = self.settings.resolve_policy(tier=tier, path=path)
        algorithm = self._resolve_algorithm(request)
        scope = f"{request.method}:{path}"

        result = await limiter.check_rate_limit(
            client_id=client_id,
            policy=policy,
            scope=scope,
            algorithm=algorithm,
        )

        if not result.allowed:
            retry_after_seconds = max(1, math.ceil(result.retry_after))
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded. Too many requests.",
                    "tier": tier,
                    "algorithm": result.algorithm,
                    "retry_after_seconds": retry_after_seconds,
                },
            )
            self._attach_headers(response, result, tier)
            response.headers["Retry-After"] = str(retry_after_seconds)
            return response

        request.state.client_id = client_id
        request.state.client_tier = tier
        request.state.rate_limit = result

        response = await call_next(request)
        self._attach_headers(response, result, tier)
        return response

    def _identify_client(self, request: Request) -> tuple[str, str]:
        """Resolve client identifier and subscription tier from API key or client IP."""
        api_key = request.headers.get("X-API-Key")
        if api_key:
            tier = self.settings.API_KEYS.get(api_key, "free")
            masked = api_key[:12] if len(api_key) > 12 else api_key
            return f"key:{masked}", tier

        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else "unknown"
        return f"ip:{client_ip}", "free"

    def _resolve_algorithm(self, request: Request) -> AlgorithmType | None:
        """Allow optional per-request algorithm selection via header."""
        header_algo = request.headers.get("X-RateLimit-Algorithm")
        if header_algo in ("token_bucket", "sliding_window"):
            return header_algo  # type: ignore[return-value]
        return None

    @staticmethod
    def _attach_headers(response: Response, result: RateLimitResult, tier: str) -> None:
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(math.ceil(result.reset_after))
        response.headers["X-RateLimit-Policy"] = f"{result.algorithm}; tier={tier}"
        if result.fallback_used:
            response.headers["X-RateLimit-Fallback"] = "fail-open"
