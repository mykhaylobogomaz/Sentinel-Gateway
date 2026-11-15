"""Application configuration and tiered rate-limiting policies."""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TierPolicy(BaseModel):
    """Defines capacity and rate thresholds for a specific client tier or endpoint."""

    capacity: int = Field(..., ge=1, description="Max burst tokens for Token Bucket.")
    refill_rate: float = Field(..., gt=0, description="Tokens replenished per second.")
    window_size: int = Field(..., ge=1, description="Sliding window duration in seconds.")
    max_requests: int = Field(..., ge=1, description="Max requests per window for Sliding Window.")


class Settings(BaseSettings):
    """Environment-driven application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "Sentinel-Gateway"
    APP_VERSION: str = "1.0.0"
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_SOCKET_TIMEOUT: float = 0.5
    FAIL_OPEN: bool = True
    DEFAULT_ALGORITHM: Literal["token_bucket", "sliding_window"] = "token_bucket"

    EXEMPT_PATHS: list[str] = [
        "/health",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
    ]

    TIER_POLICIES: dict[str, TierPolicy] = {
        "free": TierPolicy(
            capacity=10,
            refill_rate=1.0,
            window_size=60,
            max_requests=20,
        ),
        "pro": TierPolicy(
            capacity=60,
            refill_rate=10.0,
            window_size=60,
            max_requests=300,
        ),
        "internal": TierPolicy(
            capacity=500,
            refill_rate=100.0,
            window_size=60,
            max_requests=3000,
        ),
    }

    # Stricter policy applied to compute-intensive endpoints for free-tier clients
    ENDPOINT_FREE_OVERRIDES: dict[str, TierPolicy] = {
        "/api/v1/analyze": TierPolicy(
            capacity=3,
            refill_rate=0.2,
            window_size=60,
            max_requests=5,
        ),
    }

    API_KEYS: dict[str, str] = {
        "sg_free_demo_key": "free",
        "sg_pro_demo_key": "pro",
        "sg_internal_demo_key": "internal",
    }

    def resolve_policy(self, tier: str, path: str) -> TierPolicy:
        """Return the effective TierPolicy for a given client tier and request path."""
        normalized_tier = tier if tier in self.TIER_POLICIES else "free"
        if normalized_tier == "free" and path in self.ENDPOINT_FREE_OVERRIDES:
            return self.ENDPOINT_FREE_OVERRIDES[path]
        return self.TIER_POLICIES[normalized_tier]


settings = Settings()
