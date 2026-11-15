"""Distributed rate limiter engine with Redis Lua execution and fail-open resilience."""

import logging
import math
import time
from typing import Literal

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings, TierPolicy
from app.core.lua_scripts import SLIDING_WINDOW_LUA, TOKEN_BUCKET_LUA

logger = logging.getLogger("sentinel.limiter")

AlgorithmType = Literal["token_bucket", "sliding_window"]


class RateLimitResult(BaseModel):
    """Outcome of a single rate-limit evaluation."""

    allowed: bool
    limit: int
    remaining: int
    reset_after: float
    retry_after: float
    algorithm: str
    fallback_used: bool = False


class MetricsStore:
    """In-memory telemetry counters for gateway observability."""

    def __init__(self) -> None:
        self.total_requests: int = 0
        self.allowed_requests: int = 0
        self.blocked_requests: int = 0
        self.fallback_requests: int = 0
        self.redis_errors: int = 0

    def snapshot(self) -> dict[str, int | float]:
        """Return a serializable snapshot of current request metrics."""
        block_rate = (
            round((self.blocked_requests / self.total_requests) * 100, 2)
            if self.total_requests > 0
            else 0.0
        )
        return {
            "total_requests": self.total_requests,
            "allowed_requests": self.allowed_requests,
            "blocked_requests": self.blocked_requests,
            "fallback_requests": self.fallback_requests,
            "redis_errors": self.redis_errors,
            "block_rate_percent": block_rate,
        }


class RateLimiter:
    """Coordinates atomic Redis Lua script execution and graceful degradation."""

    def __init__(self, redis: Redis, app_settings: Settings, metrics: MetricsStore) -> None:
        self.redis = redis
        self.settings = app_settings
        self.metrics = metrics
        self._token_bucket_script = self.redis.register_script(TOKEN_BUCKET_LUA)
        self._sliding_window_script = self.redis.register_script(SLIDING_WINDOW_LUA)

    async def check_rate_limit(
        self,
        client_id: str,
        policy: TierPolicy,
        scope: str,
        algorithm: AlgorithmType | None = None,
        now: float | None = None,
    ) -> RateLimitResult:
        """Evaluate whether a request is permitted under the selected algorithm and policy."""
        algo: AlgorithmType = algorithm or self.settings.DEFAULT_ALGORITHM
        current_ts = now if now is not None else time.time()
        self.metrics.total_requests += 1

        try:
            if algo == "sliding_window":
                result = await self._evaluate_sliding_window(client_id, policy, scope, current_ts)
            else:
                result = await self._evaluate_token_bucket(client_id, policy, scope, current_ts)

            if result.allowed:
                self.metrics.allowed_requests += 1
            else:
                self.metrics.blocked_requests += 1
            return result

        except (RedisError, TimeoutError, OSError) as exc:
            self.metrics.redis_errors += 1
            self.metrics.fallback_requests += 1
            limit = policy.max_requests if algo == "sliding_window" else policy.capacity

            if self.settings.FAIL_OPEN:
                self.metrics.allowed_requests += 1
                logger.warning(
                    "Redis unavailable (%s); fail-open enabled, allowing request for %s",
                    exc.__class__.__name__,
                    client_id,
                )
                return RateLimitResult(
                    allowed=True,
                    limit=limit,
                    remaining=limit,
                    reset_after=0.0,
                    retry_after=0.0,
                    algorithm=algo,
                    fallback_used=True,
                )

            self.metrics.blocked_requests += 1
            logger.error(
                "Redis unavailable (%s); fail-closed enabled, blocking request for %s",
                exc.__class__.__name__,
                client_id,
            )
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                reset_after=5.0,
                retry_after=5.0,
                algorithm=algo,
                fallback_used=True,
            )

    async def _evaluate_token_bucket(
        self,
        client_id: str,
        policy: TierPolicy,
        scope: str,
        now: float,
    ) -> RateLimitResult:
        key = f"sentinel:tb:{client_id}:{scope}"
        raw = await self._token_bucket_script(
            keys=[key],
            args=[policy.capacity, policy.refill_rate, now, 1],
        )
        allowed = bool(int(raw[0]))
        remaining = int(raw[1])
        reset_after = round(float(raw[2]), 3)
        retry_after = round(float(raw[3]), 3)

        return RateLimitResult(
            allowed=allowed,
            limit=policy.capacity,
            remaining=remaining,
            reset_after=reset_after,
            retry_after=retry_after,
            algorithm="token_bucket",
        )

    async def _evaluate_sliding_window(
        self,
        client_id: str,
        policy: TierPolicy,
        scope: str,
        now: float,
    ) -> RateLimitResult:
        window_id = math.floor(now / policy.window_size)
        prev_key = f"sentinel:sw:{client_id}:{scope}:{window_id - 1}"
        curr_key = f"sentinel:sw:{client_id}:{scope}:{window_id}"

        raw = await self._sliding_window_script(
            keys=[prev_key, curr_key],
            args=[policy.window_size, policy.max_requests, now],
        )
        allowed = bool(int(raw[0]))
        remaining = int(raw[1])
        reset_after = round(float(raw[2]), 3)
        retry_after = round(float(raw[3]), 3)

        return RateLimitResult(
            allowed=allowed,
            limit=policy.max_requests,
            remaining=remaining,
            reset_after=reset_after,
            retry_after=retry_after,
            algorithm="sliding_window",
        )
