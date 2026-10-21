"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("agentcart.config")

_INSECURE_SECRETS = {"", "change-me", "changeme", "secret", "dev"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    app_name: str = "AgentCart"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://agentcart:agentcart@localhost:5433/agentcart"
    db_echo: bool = False

    # --- auth ---
    jwt_secret: str = ""
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_expire_minutes: int = Field(default=60, ge=5, le=60 * 24)

    # --- http ---
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = 10
    rate_limit_chat_per_minute: int = 20
    rate_limit_write_per_minute: int = 60
    # Reverse proxies in front of the app that append to X-Forwarded-For (nginx locally, Render's
    # load balancer in production). The client IP is the entry this many places from the right;
    # entries further left are client-supplied and ignored. 0 = use the socket peer address.
    trusted_proxy_hops: int = Field(default=1, ge=0, le=5)

    # --- AI providers ---
    llm_provider: Literal["mock", "ollama", "openai_compatible", "anthropic"] = "mock"
    llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    agent_deadline_seconds: float = Field(default=45.0, gt=0, le=300)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    hosted_llm_base_url: str = "https://api.groq.com/openai/v1"
    hosted_llm_api_key: str = ""
    hosted_llm_model: str = "llama-3.1-8b-instant"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"

    # --- ranking weights (normalised at use) ---
    rank_weight_budget: float = Field(default=0.25, ge=0)
    rank_weight_spec: float = Field(default=0.30, ge=0)
    rank_weight_rating: float = Field(default=0.20, ge=0)
    rank_weight_preference: float = Field(default=0.25, ge=0)

    # --- commerce rules ---
    max_item_quantity: int = Field(default=10, ge=1, le=100)
    max_cart_lines: int = Field(default=30, ge=1, le=200)
    shipping_fee_inr: int = Field(default=99, ge=0)
    free_shipping_threshold_inr: int = Field(default=999, ge=0)
    checkout_quote_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    agent_action_ttl_minutes: int = Field(default=30, ge=1, le=1440)

    # --- persistent agent tasks ---
    task_step_max_attempts: int = Field(default=3, ge=1, le=10)
    task_retry_backoff_seconds: float = Field(default=0.5, ge=0, le=30)  # doubles per retry
    task_lease_seconds: int = Field(default=120, ge=5, le=3600)
    task_max_active_per_user: int = Field(default=5, ge=1, le=100)
    # --- planning loop (shopping_agent tasks) ---
    task_max_iterations: int = Field(default=8, ge=1, le=50)  # planner decisions per task, all runs
    task_max_tool_calls: int = Field(default=12, ge=1, le=100)  # executed tool calls per task, all runs
    task_max_invalid_decisions: int = Field(default=2, ge=0, le=20)  # rejected decisions tolerated
    task_deadline_seconds: float = Field(default=60.0, gt=0, le=600)  # wall-clock budget per run

    @model_validator(mode="after")
    def _secure_defaults(self) -> Settings:
        if self.jwt_secret.strip().lower() in _INSECURE_SECRETS or len(self.jwt_secret) < 32:
            if self.environment == "production":
                raise ValueError("JWT_SECRET must be set to a random value of at least 32 characters in production")
            # Development/test: generate an ephemeral secret instead of shipping a known default.
            # Tokens are invalidated whenever the process restarts.
            self.jwt_secret = secrets.token_urlsafe(48)
            if self.environment == "development":
                logger.warning("JWT_SECRET not set; using an ephemeral random secret for this process")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        # Browsers send Origin as scheme://host[:port] with no path or trailing slash, and CORS
        # matching is exact. Normalise values pasted from an address bar ("https://x.app/") or
        # with stray quotes so they still match.
        origins = (o.strip().strip("\"'").strip().rstrip("/") for o in self.cors_origins.split(","))
        return [o for o in origins if o]

    @property
    def ranking_weights(self) -> dict[str, float]:
        return {
            "budget": self.rank_weight_budget,
            "spec": self.rank_weight_spec,
            "rating": self.rank_weight_rating,
            "preference": self.rank_weight_preference,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
