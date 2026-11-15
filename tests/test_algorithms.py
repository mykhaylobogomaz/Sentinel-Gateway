"""Unit and concurrency tests for Token Bucket and Sliding Window Lua scripts."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings, TierPolicy
from app.core.limiter import MetricsStore, RateLimiter


@pytest.mark.asyncio
async def test_token_bucket_burst_and_refill(rate_limiter: RateLimiter) -> None:
    """Verify Token Bucket allows initial burst, blocks excess, and refills over time."""
    policy = TierPolicy(capacity=3, refill_rate=1.0, window_size=60, max_requests=10)
    t0 = 1_700_000_000.0

    # Consume all 3 burst tokens at t0
    for expected_remaining in (2, 1, 0):
        res = await rate_limiter.check_rate_limit(
            client_id="client-1",
            policy=policy,
            scope="GET:/test",
            algorithm="token_bucket",
            now=t0,
        )
        assert res.allowed is True
        assert res.remaining == expected_remaining

    # 4th request at t0 must be blocked
    blocked = await rate_limiter.check_rate_limit(
        client_id="client-1",
        policy=policy,
        scope="GET:/test",
        algorithm="token_bucket",
        now=t0,
    )
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after == pytest.approx(1.0, rel=1e-2)

    # Advance clock by 2.0 seconds -> 2 tokens replenished
    recovered = await rate_limiter.check_rate_limit(
        client_id="client-1",
        policy=policy,
        scope="GET:/test",
        algorithm="token_bucket",
        now=t0 + 2.0,
    )
    assert recovered.allowed is True
    assert recovered.remaining == 1


@pytest.mark.asyncio
async def test_sliding_window_enforcement_and_rollover(rate_limiter: RateLimiter) -> None:
    """Verify Sliding Window enforces max requests and decays previous window weight."""
    policy = TierPolicy(capacity=10, refill_rate=1.0, window_size=10, max_requests=4)
    # Start right at window boundary (multiple of 10)
    t0 = 1_700_000_000.0

    for i in range(4):
        res = await rate_limiter.check_rate_limit(
            client_id="client-sw",
            policy=policy,
            scope="GET:/sw",
            algorithm="sliding_window",
            now=t0 + 0.1 * i,
        )
        assert res.allowed is True

    # 5th request inside same window is rejected
    blocked = await rate_limiter.check_rate_limit(
        client_id="client-sw",
        policy=policy,
        scope="GET:/sw",
        algorithm="sliding_window",
        now=t0 + 1.0,
    )
    assert blocked.allowed is False
    assert blocked.remaining == 0

    # Move halfway into next window (t0 + 15.0): prev_weight = 0.5 -> weighted count = 4 * 0.5 = 2.0
    # So 2 more requests should succeed, and the 3rd should block
    res_next_1 = await rate_limiter.check_rate_limit(
        client_id="client-sw",
        policy=policy,
        scope="GET:/sw",
        algorithm="sliding_window",
        now=t0 + 15.0,
    )
    res_next_2 = await rate_limiter.check_rate_limit(
        client_id="client-sw",
        policy=policy,
        scope="GET:/sw",
        algorithm="sliding_window",
        now=t0 + 15.1,
    )
    res_next_3 = await rate_limiter.check_rate_limit(
        client_id="client-sw",
        policy=policy,
        scope="GET:/sw",
        algorithm="sliding_window",
        now=t0 + 15.2,
    )
    assert res_next_1.allowed is True
    assert res_next_2.allowed is True
    assert res_next_3.allowed is False


@pytest.mark.asyncio
async def test_atomic_concurrency_no_race_condition(rate_limiter: RateLimiter) -> None:
    """Fire 25 concurrent requests against a capacity-5 bucket; strictly 5 must succeed."""
    policy = TierPolicy(capacity=5, refill_rate=0.01, window_size=60, max_requests=5)
    t0 = 1_700_000_100.0

    results = await asyncio.gather(
        *[
            rate_limiter.check_rate_limit(
                client_id="concurrent-client",
                policy=policy,
                scope="GET:/race",
                algorithm="token_bucket",
                now=t0,
            )
            for _ in range(25)
        ]
    )

    allowed_count = sum(1 for r in results if r.allowed)
    blocked_count = sum(1 for r in results if not r.allowed)
    assert allowed_count == 5
    assert blocked_count == 20


@pytest.mark.asyncio
async def test_fail_open_graceful_degradation() -> None:
    """When Redis raises ConnectionError and FAIL_OPEN=True, traffic is permitted."""
    broken_redis = AsyncMock()
    broken_redis.register_script = lambda _: AsyncMock(
        side_effect=RedisConnectionError("Connection refused")
    )
    metrics = MetricsStore()
    limiter = RateLimiter(
        redis=broken_redis,
        app_settings=Settings(FAIL_OPEN=True),
        metrics=metrics,
    )
    policy = TierPolicy(capacity=5, refill_rate=1.0, window_size=60, max_requests=10)

    result = await limiter.check_rate_limit("client-down", policy, "GET:/api")
    assert result.allowed is True
    assert result.fallback_used is True
    assert metrics.redis_errors == 1
    assert metrics.fallback_requests == 1
