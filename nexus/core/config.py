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

    # Alerts — optional delivery endpoints. Empty = the channel is a no-op (alert still persists
    # in-app). ``alert_slack_webhook_url`` is a Slack Incoming Webhook; ``alert_email_sender`` is
    # the (everifier-validated, in prod) From address the email channel sends as.
    alert_webhook_url: str = ""
    alert_slack_webhook_url: str = ""
    alert_email_sender: str = ""

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

    # Multi-source data substrate (DataSourceRegistry). Ordered, comma-separated source lists
    # form the priority waterfall (InfoJoy -> web search -> Apify). Defaults preserve today's
    # behavior — web-backed company discovery on, everything else an offline stub — so CI stays
    # zero-network and no shipped behavior silently regresses.
    company_search_sources: str = "search"     # ordered: e.g. "infojoy,search,apify"
    enrich_sources: str = "stub"               # ordered enrichment providers
    signal_sources: str = "demo"               # ordered signal sources
    search_provider: str = "duckduckgo"        # web-search backend: duckduckgo|exa|brave|serper
    research_provider: str = "stub"            # account-research backend
    email_verify_provider: str = "stub"        # email-deliverability backend
    email_verify_url: str = "http://158.69.113.127:8080/v0/check_email"
    email_verify_timeout_s: float = 20.0
    # Contact sourcing (sub-project B): net-new contact providers + the verifying email
    # finder. Defaults stay offline (stub) so CI is zero-network; activation is one env line.
    email_finder_max_candidates: int = 5        # permutation cap per contact
    contact_search_sources: str = "stub"        # ordered net-new contact providers
    campaign_sourcing_enabled: bool = True       # inline auto-retry on SKIP_NO_CONTACT
    campaign_sourced_min_send_confidence: float = 0.5  # bar a sourced address must clear to send
    crm_provider: str = "stub"                 # outbound CRM connector: stub|salesforce|hubspot
    # Channel & Cadence (sub-project C): multi-touch email cadence engine. Disabled by
    # default (safe opt-in, like campaign_sourcing) so the advance tick is a no-op until a
    # deployment turns it on with one env line.
    cadence_enabled: bool = False             # master switch for the advance tick
    cadence_tick_interval_s: int = 60         # production due-scan cadence (seconds)
    cadence_batch_size: int = 100             # max enrollments claimed per tick per worker
    cadence_max_duration_days: int = 30       # duration-cap safety bound (mid-sequence stop)

    # Continuous Automation (sub-project D): autonomous heartbeat that drives the recurring
    # GTM loop (account refresh + cadence advance). OFF by default (safe opt-in, like
    # cadence_enabled) so the test suite stays deterministic and zero-network.
    automation_enabled: bool = False            # global master switch for the heartbeat
    automation_tick_interval_s: int = 60        # heartbeat period (seconds)
    account_refresh_interval_s: int = 21600     # staleness threshold before re-processing (6h)
    account_refresh_batch_size: int = 100       # max accounts claimed per tick across tenants

    # CRM Auto-Sync (sub-project E): continuously push account state to the configured CRM.
    # OFF by default (safe opt-in, like automation_enabled) so the suite is deterministic and
    # zero-network (tests use the recording stub connector). Change-aware via Account.crm_synced_at,
    # so there is no interval knob — only stale/changed accounts are pushed.
    crm_sync_enabled: bool = False        # global master switch for auto-sync
    crm_sync_batch_size: int = 100        # max accounts claimed per heartbeat sweep

    # Synthetic demo signals (DemoSignalSource) in the DEFAULT ingestion pipeline. They make
    # a fresh local workspace come alive without network, but in production they would show
    # reps fabricated funding/hiring events as if they were real — deployments must set
    # NEXUS_DEMO_SIGNALS_ENABLED=false so only genuine sources feed the pipeline.
    demo_signals_enabled: bool = True

    # Daily digest (SDR adoption): once per interval, summarize the last day's GTM activity
    # into an email-channel alert per opted-in tenant. Rides the automation heartbeat (only
    # ticks while automation_enabled) and is idempotent per interval, so the scheduler can
    # enqueue it every tick without double-sending.
    digest_interval_hours: int = 24

    # Hosted web-search API keys, consumed only when `search_provider` selects that engine.
    # Secrets: set via NEXUS_*_API_KEY env (or a gitignored .env). NEVER commit a real value.
    # A selected engine with no key degrades to keyless DuckDuckGo so search keeps working.
    exa_api_key: str = ""
    brave_api_key: str = ""
    serper_api_key: str = ""

    # Orchestration engine
    orch_max_attempts: int = 2          # per-step retries before a step is marked failed
    orch_max_steps: int = 200           # hard cap on plan size (runaway / cost guard)
    orch_autonomy: Literal["gated", "auto"] = "gated"  # "gated" = outbound needs approval

    # Conversational orchestrator (chat) — token-frugal context envelope.
    orch_chat_token_budget: int = 1200       # hard cap on the per-turn LLM payload
    orch_chat_recency_window: int = 4         # last K raw messages kept verbatim
    orch_chat_summary_token_cap: int = 150    # rolling summary ceiling (approx tokens)
    discovery_max_candidates: int = 25        # cap on discovery result list size
    campaign_preview_sample: int = 3          # drafted targets shown at the approval gate

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @staticmethod
    def _csv_list(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def company_search_source_list(self) -> list[str]:
        return self._csv_list(self.company_search_sources)

    @property
    def enrich_source_list(self) -> list[str]:
        return self._csv_list(self.enrich_sources)

    @property
    def signal_source_list(self) -> list[str]:
        return self._csv_list(self.signal_sources)

    @property
    def contact_search_source_list(self) -> list[str]:
        return self._csv_list(self.contact_search_sources)

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
