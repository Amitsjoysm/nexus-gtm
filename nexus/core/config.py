"""Application configuration, loaded from environment with safe local defaults."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET = "dev-insecure-secret-change-me-please-32chars"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEXUS_", env_file=".env", extra="ignore"
    )

    env: Literal["local", "test", "staging", "prod"] = "local"

    # Auth
    secret_key: str = _INSECURE_SECRET
    access_token_ttl_min: int = 60
    jwt_algorithm: str = "HS256"

    # CORS — comma-separated origins (e.g. a Chrome extension or external SPA). Empty = same-origin.
    cors_origins: str = ""

    # Per-source timeout (seconds) so a slow signal source can't hang a request.
    source_timeout_s: float = 8.0

    # Alerts — optional outbound webhook for "webhook"-channel alerts. Empty = log only.
    alert_webhook_url: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./nexus.db"

    # Task queue
    queue_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # LLM
    llm_provider: Literal["stub", "openai_compat"] = "stub"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # Browser / scraping
    browser_provider: Literal["auto", "scrapling", "duckduckgo", "cloak"] = "auto"
    cloak_cdp_url: str = "http://localhost:8080"

    # Orchestration engine
    orch_max_attempts: int = 2          # per-step retries before a step is marked failed
    orch_max_steps: int = 200           # hard cap on plan size (runaway / cost guard)
    orch_autonomy: Literal["gated", "auto"] = "gated"  # "gated" = outbound needs approval

    # Conversational orchestrator (chat) — token-frugal context envelope.
    orch_chat_token_budget: int = 1200       # hard cap on the per-turn LLM payload
    orch_chat_recency_window: int = 4         # last K raw messages kept verbatim
    orch_chat_summary_token_cap: int = 150    # rolling summary ceiling (approx tokens)
    discovery_max_candidates: int = 25        # cap on discovery result list size

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _reject_insecure_prod(self) -> "Settings":
        if self.env in ("staging", "prod") and self.secret_key == _INSECURE_SECRET:
            raise ValueError(
                "NEXUS_SECRET_KEY must be set to a strong value outside local/test"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
