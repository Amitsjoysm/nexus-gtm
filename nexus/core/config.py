"""Application configuration, loaded from environment with safe local defaults."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
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

    # System transactional email (OTP / verification), distinct from per-tenant cadence SMTP.
    # A single system mailbox sends signup codes. Credentials come from env (NEXUS_SYSTEM_SMTP_*)
    # and live only in the gitignored deploy/.env — never in source. Empty = OTP email is a no-op.
    system_smtp_provider: str = "gmail"     # preset host/port (see email_sender.PROVIDER_PRESETS)
    system_smtp_username: str = ""
    system_smtp_password: str = ""          # app password; env/secret only
    system_smtp_from: str = ""              # defaults to the username
    system_smtp_from_name: str = "Infojoy GTM"

    # Two-step OTP registration. When enabled, /auth/signup is gated behind email verification:
    # the client calls /auth/register/start -> /auth/register/verify. Off by default so local/dev
    # and the offline test suite keep the single-step path. The OTP is stored only as an
    # HMAC-SHA256 hash (one-way), keyed by ``otp_secret`` (falls back to ``secret_key``).
    otp_registration_enabled: bool = False
    otp_length: int = 6
    otp_ttl_s: int = 600                     # code lifetime (10 min)
    otp_max_attempts: int = 5                # wrong tries before the pending registration is voided
    otp_resend_cooldown_s: int = 60          # min seconds between code emails for one registration
    otp_secret: str = ""                     # HMAC key for hashing OTPs; falls back to secret_key

    # Auth abuse protection. Off by default so local/dev and the offline test suite (which make
    # many rapid signup/login calls) are unaffected; enable in production. A per-client-IP
    # sliding window over the auth endpoints (login / register / password reset). Caddy + Valkey
    # provide the stronger, cross-process layer in production; this is in-process defense-in-depth.
    auth_rate_limit_enabled: bool = False
    auth_rate_limit_max: int = 10            # max attempts per window per IP per bucket
    auth_rate_limit_window_s: int = 60

    # Password reset (forgot password). A single-use, URL-safe token, stored only as an HMAC hash,
    # emailed as a link to the verified account holder. Generic responses prevent email enumeration.
    password_reset_ttl_s: int = 3600         # link lifetime (1 hour)
    password_reset_cooldown_s: int = 60      # min seconds between reset emails for one account
    # Public base URL for building links in transactional email (e.g. https://app.infojoy.com).
    # Empty falls back to a relative path; set in production.
    app_base_url: str = ""

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

    # LLM. "auto" builds a runtime fallback chain (Anthropic -> Groq -> OpenAI-compat -> stub)
    # from whichever keys are present, so a provider outage degrades instead of erroring.
    llm_provider: Literal["stub", "openai_compat", "anthropic", "groq", "auto"] = "stub"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    # Anthropic (preferred when keyed) — native /v1/messages API.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    # Groq (OpenAI-compatible) — the fast secondary LLM, used after Anthropic.
    groq_api_key: str = ""
    # Optional rotation pool (comma-separated). On a 429 the provider switches to the next key,
    # so bursty LLM load rides out a single key's rate limit instead of degrading to the stub.
    groq_api_keys: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"

    # Browser / scraping
    browser_provider: Literal["auto", "scrapling", "duckduckgo", "cloak"] = "auto"
    cloak_cdp_url: str = "http://localhost:8080"

    # Multi-source data substrate (DataSourceRegistry). Ordered, comma-separated source lists
    # form the priority waterfall (InfoJoy -> web search -> Apify). Defaults preserve today's
    # behavior — web-backed company discovery on, everything else an offline stub — so CI stays
    # zero-network and no shipped behavior silently regresses.
    company_search_sources: str = "search"     # ordered: e.g. "infojoy,search,apify"
    enrich_sources: str = "stub"               # ordered enrichment providers
    # Fill blank account firmographics (industry/size/country/tech/description) from the web
    # (Exa→DuckDuckGo + LLM) when no premium data provider is configured. Off by default so CI
    # stays zero-network; the pipeline only enriches accounts that are missing firmographics.
    account_enrich_enabled: bool = False
    # Look-alike finder: enrich candidate companies (bounded, concurrent, best-effort) so similarity
    # can be scored on real firmographics (industry/size/geo/revenue/tech), not just snippet text.
    # Defaults to following account_enrich_enabled (set explicitly to override); CI stays offline.
    lookalike_enrich_candidates: bool | None = None
    lookalike_enrich_max: int = 8              # max candidates to enrich per find (latency bound)
    lookalike_enrich_concurrency: int = 5      # concurrent enrichments
    # Blend: final look-alike score = w·similarity-to-seed + (1-w)·ICP-fit. Seed resemblance leads.
    lookalike_similarity_weight: float = 0.8
    # Minimum signal strength (0..1) that creates an Inbox task. Weaker signals (e.g. a generic
    # press mention, 0.4) still persist to the account timeline and feed Plays, but don't clutter
    # the rep's daily task list — only meaningful events (funding/hiring/intent) become tasks.
    inbox_min_signal_strength: float = 0.5
    signal_sources: str = "demo"               # ordered signal sources
    search_provider: str = "duckduckgo"        # web-search backend: duckduckgo|exa|brave|serper
    research_provider: str = "stub"            # account-research backend
    email_verify_provider: str = "stub"        # email-deliverability backend
    # Reacher verifier endpoint. In production point this at an HTTPS URL fronted by your
    # reverse proxy (the container can reach :443 but not Reacher's raw :8080), e.g.
    # https://verify.example.com/v0/check_email. The default is the local-only HTTP port.
    email_verify_url: str = "http://158.69.113.127:8080/v0/check_email"
    email_verify_timeout_s: float = 20.0
    # Optional value sent as the HTTP ``Authorization`` header on every verify request, so a
    # publicly-exposed HTTPS verifier endpoint isn't an open relay. Blank = no header (offline
    # default). Example: "Bearer s3cr3t" or "Basic dXNlcjpwYXNz".
    email_verify_auth_header: str = ""
    # Contact sourcing (sub-project B): net-new contact providers + the verifying email
    # finder. Defaults stay offline (stub) so CI is zero-network; activation is one env line.
    email_finder_max_candidates: int = 12       # permutation cap per contact (10 patterns + headroom)
    contact_search_sources: str = "stub"        # ordered net-new contact providers
    campaign_sourcing_enabled: bool = True       # inline auto-retry on SKIP_NO_CONTACT
    campaign_sourced_min_send_confidence: float = 0.5  # bar a sourced address must clear to send
    crm_provider: str = "stub"                 # outbound CRM connector: stub|salesforce|hubspot
    hubspot_access_token: str = ""             # HubSpot private-app token (when crm_provider=hubspot)
    hubspot_api_base: str = "https://api.hubapi.com"  # override for region/proxy/testing
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

    # Person-level personalization (sub-project I): a social-enrichment provider that fetches a
    # contact's posts/comments/profile (LinkedIn etc.) to deepen email/call personalization.
    # Default stub (no-op, offline). A future Apify actor plugs in via NEXUS_PERSONALIZATION_PROVIDER
    # — no caller change needed; fetched insights land on contact.custom_fields['personalization'].
    personalization_provider: str = "stub"   # stub | apify | ...
    personalization_max_posts: int = 3       # most-recent posts referenced in a message

    # Daily ICP Auto-Discovery (sub-project H): each interval, for opted-in tenants, discover
    # net-new companies and add ONLY strict ICP matches (icp-fit >= min_fit). OFF by default
    # (network + cost); rides the automation heartbeat + per-tenant automation_enabled.
    icp_discovery_enabled: bool = False
    icp_discovery_daily_count: int = 20        # target net-new strict matches per interval (SDR feed)
    icp_discovery_min_fit: int = 70            # strict ICP-fit threshold 0-100; below = discarded
    icp_discovery_interval_hours: int = 24     # once per day per tenant
    icp_discovery_pool_multiplier: int = 5     # search pool = daily_count * this (capped at Exa's 100)
    # Crawl candidates' firmographics (headcount/tech/revenue) AFTER search, BEFORE scoring, so the
    # ICP-fit score can differentiate them (Exa returns domain/industry/geo but not headcount/tech →
    # otherwise every candidate scores the same). None → follows account_enrich_enabled. Bounded so
    # the daily crawl cost per tenant is capped; best-effort, so a crawl failure never blocks discovery.
    icp_discovery_enrich_candidates: bool | None = None
    icp_discovery_enrich_max: int = 40         # max candidates to web-enrich per run (cost bound)
    icp_discovery_enrich_concurrency: int = 5  # concurrent enrichments

    # Cold Calling (sub-project G): AI call scripts + a call queue + dispositions + cadence
    # "call" touches. The workflow is offline-safe (no telephony in v1), so it's on by default.
    # Real dialing/recording is opt-in via ``telephony_provider`` (stub until a deployment sets it),
    # mirroring email_verify_provider / crm_provider — no infra needed for v1.
    calling_enabled: bool = True
    telephony_provider: str = "stub"        # stub | twilio | ...  (tier 2)
    telephony_from_number: str = ""         # caller ID used when a real provider is enabled
    call_queue_default_limit: int = 50      # default page size for the call queue

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

    # Prometheus /metrics. OFF by default: the instrumentator wraps every request, so a
    # FastAPI/instrumentator version mismatch becomes a 500 on every endpoint. Enable only
    # with compatible pinned versions and a scraper in front.
    metrics_enabled: bool = False

    # Hosted web-search API keys, consumed only when `search_provider` selects that engine.
    # Secrets: set via NEXUS_*_API_KEY env (or a gitignored .env). NEVER commit a real value.
    # A selected engine with no key degrades to keyless DuckDuckGo so search keeps working.
    exa_api_key: str = ""
    # Optional rotation pool (comma-separated). The provider switches to the next key on a 429,
    # so bursty discovery rides out a single key's rate limit instead of going dark.
    exa_api_keys: str = ""
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
    discovery_contacts_per_account: int = 5   # buying-committee size sourced per account
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

    def _key_pool(self, primary: str, pool_csv: str) -> list[str]:
        """Primary key first, then the rotation pool — deduped, blanks dropped."""
        keys = [(primary or "").strip()] + self._csv_list(pool_csv)
        seen: set[str] = set()
        out: list[str] = []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out

    @property
    def exa_api_key_list(self) -> list[str]:
        return self._key_pool(self.exa_api_key, self.exa_api_keys)

    @property
    def groq_api_key_list(self) -> list[str]:
        return self._key_pool(self.groq_api_key, self.groq_api_keys)

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
