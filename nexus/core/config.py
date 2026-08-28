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
    system_smtp_from_name: str = "InfoJoy GTM"

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

    # Multi-factor authentication (opt-in per user; enrolment never gates a user who has not
    # confirmed one). TOTP is RFC 6238 with the standard 30s step; the "email" method is the same
    # primitive on a longer step, so a mailed code lives 5-10 minutes (step + ±1 drift) and shares
    # the TOTP replay guard instead of needing its own in-flight row.
    mfa_issuer: str = "InfoJoy GTM"          # what shows up in the authenticator app
    mfa_totp_step_s: int = 30
    mfa_totp_digits: int = 6
    mfa_totp_drift_steps: int = 1            # clock skew tolerated either side of now
    mfa_email_code_step_s: int = 300         # mailed-code step; ±1 drift => 5-10 min validity
    mfa_recovery_code_count: int = 10
    mfa_challenge_ttl_s: int = 300           # life of the single-purpose second-factor challenge
    mfa_max_attempts: int = 5                # wrong codes before the factor locks
    mfa_lockout_s: int = 900                 # how long it stays locked (15 min)
    # Dedicated at-rest key for TOTP seeds. Empty derives one from ``secret_key`` (so seeds are
    # always encrypted). Separate from ``network_token_enc_key`` so the two rotate independently.
    mfa_secret_enc_key: str = ""

    # Auth abuse protection. ON by default (M13): an unthrottled login endpoint is a credential
    # -stuffing target, and "secure once someone remembers to enable it" is not a security
    # posture. A per-client-IP sliding window over login / register / password reset / MFA
    # verification. Caddy + Valkey provide the stronger cross-process layer in production; this
    # is in-process defense-in-depth.
    #
    # The offline suite makes many rapid auth calls, so tests that need to exceed the window opt
    # out explicitly via the `no_auth_rate_limit` fixture rather than the default being weakened
    # for everyone.
    auth_rate_limit_enabled: bool = True
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

    # App-layer security headers (defense-in-depth). Set-if-absent, so when Caddy (or any proxy)
    # already sets them they are left untouched; when the app is exposed directly they are still
    # present. HSTS is only emitted outside local/test (never force HTTPS on plain-HTTP localhost).
    security_headers_enabled: bool = True

    # Idempotency for mutating POSTs. Off by default (opt-in): when on, a POST carrying an
    # ``Idempotency-Key`` header is de-duplicated — a retry with the same key replays the first
    # response instead of re-running the work. Uses the same backend as the task queue (Redis in
    # production for cross-worker dedup; in-process for single-node dev). No header ⇒ no change.
    idempotency_enabled: bool = False
    idempotency_ttl_s: int = 86400  # how long a key's response is remembered (24h)

    # Max accepted request body. Rejects oversized bodies with 413 before they are buffered — a
    # cheap DoS guard. Generous enough for the CSV upload endpoints (LinkedIn connections, custom
    # fields); tune down if you have no large uploads. 0 disables the check.
    max_request_body_bytes: int = 10_000_000  # 10 MB

    # Per-source timeout (seconds) so a slow signal source can't hang a request.
    source_timeout_s: float = 8.0

    # Alerts — optional delivery endpoints. Empty = the channel is a no-op (alert still persists
    # in-app). ``alert_slack_webhook_url`` is a Slack Incoming Webhook; ``alert_email_sender`` is
    # the (everifier-validated, in prod) From address the email channel sends as.
    alert_webhook_url: str = ""
    alert_slack_webhook_url: str = ""
    alert_email_sender: str = ""
    # Email via SMTP (optional). With no host set, the email channel stays the offline stub, so
    # default behaviour is unchanged; set host + to-address to actually send.
    alert_smtp_host: str = ""
    alert_smtp_port: int = 587
    alert_smtp_username: str = ""
    alert_smtp_password: str = ""
    alert_email_to: str = ""
    # Telegram delivery (optional): a bot token + default chat id. Empty = channel is a no-op.
    alert_telegram_bot_token: str = ""
    alert_telegram_chat_id: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./nexus.db"

    # Connection-pool sizing (Postgres only; SQLite ignores both). Peak connections per PROCESS
    # is `db_pool_size + db_max_overflow`, plus the small platform pool below — and a managed
    # Postgres has a hard, SKU-dependent `max_connections` that is easy to exceed without
    # noticing. The defaults preserve the previous hardcoded values exactly, so nothing changes
    # for an existing deployment; they exist so a small instance can be tuned DOWN in config
    # rather than by editing code or by paying for a larger SKU.
    #
    # Budget the whole fleet, not one process, and remember a rolling deploy runs the old and
    # new revisions SIMULTANEOUSLY — app connections roughly double for the length of a release,
    # which is why this reads fine in steady state and then fails during a deploy:
    #
    #   peak = app_replicas x processes x (pool + overflow + platform_pool + platform_overflow)
    #          x 2 during a rollout, + the worker's single process
    #
    # See docs/deployment/06-POSTGRESQL.md for the sizing table.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    # Platform (RLS-bypassing, cross-tenant) reads are rare compared with tenant traffic, so this
    # pool stays small — but it is still charged against the same server-wide connection limit.
    db_platform_pool_size: int = 2
    db_platform_max_overflow: int = 3

    # Task queue
    queue_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # Job durability (M11). A handler exception used to be logged and dropped, which lost
    # one-shot work (process_account, campaign sends, orchestration runs) silently. Failures are
    # now retried with jittered exponential backoff and finally parked in `dead_letter_jobs`.
    # The kill switch only disables the RETRY: an exhausted job is still dead-lettered, so
    # turning this off degrades to "fail fast, keep the evidence" — never back to losing work.
    job_retry_enabled: bool = True
    job_retry_base_delay_s: float = 2.0      # first retry waits ~2s (±25% jitter)
    job_retry_max_delay_s: float = 300.0     # ceiling, so a long outage backs off to ~5m

    # LLM. "auto" builds a runtime fallback chain (Anthropic -> Groq -> OpenAI-compat -> stub)
    # from whichever keys are present, so a provider outage degrades instead of erroring.
    llm_provider: Literal["stub", "openai_compat", "anthropic", "groq", "auto"] = "stub"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    # Must live in the DEFAULT PROVIDER's namespace. Every real deployment sets
    # NEXUS_LLM_PROVIDER=auto, which builds a **Groq** chain — so an OpenAI model name here is an
    # HTTP 404 `model_not_found` on every completion, and `GroqLLMProvider` returns '' rather than
    # raising. Measured live on 2026-08-27: website analysis, Suggest Titles, contact extraction
    # and personalization were all silently dead, reported as four separate bugs.
    # Pinned by tests/test_llm_model_default.py.
    llm_model: str = "openai/gpt-oss-120b"
    # Blended $/1k tokens used to attribute real COGS to a metered action. Config, never a
    # constant: providers reprice, and margin reporting must follow without a redeploy.
    llm_usd_per_1k_tokens: float = 0.0006
    # Owner-role connection for cross-tenant platform work (staff console, payment webhooks).
    # The app itself connects as the least-privilege RLS-bound role; see
    # nexus.core.db.get_platform_sessionmaker for why those few paths need this.
    db_owner_url: str = ""
    # Payments. Default `noop` moves no money and lets the whole lifecycle run offline; `stripe`
    # is inert until stripe_secret_key is set, and says so rather than faking success.
    payment_provider: Literal["noop", "stripe"] = "noop"
    # Days after each failed collection before the next attempt. Config so finance can tune
    # recovery without a deploy; retrying faster than this damages authorization rates.
    billing_dunning_schedule_days: str = "1,3,7"
    billing_dunning_enabled: bool = True
    # Ceiling for a `credits.grant.capped` holder (support). Goodwill credits are the most
    # common support action, so forcing an escalation for every one makes the escalation a
    # rubber stamp; a ceiling keeps the blast radius small while leaving the workflow usable.
    billing_support_credit_cap: float = 1000.0
    stripe_secret_key: str = ""
    # Ask Stripe to compute sales tax / VAT at Checkout. OFF by default and deliberately so:
    # Stripe REJECTS `automatic_tax` outright on an account without Stripe Tax configured, so a
    # default of True would break checkout on every deployment that has not set it up. Turning it
    # on also makes Checkout collect a billing address, because a rate cannot be computed without
    # one.
    stripe_automatic_tax: bool = False
    stripe_webhook_secret: str = ""
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
    # Post-completion re-alert cool-down: after a rep marks an account's task done, a NEW inbox
    # task for that account is suppressed for this many days (news churn re-covers the same event
    # under fresh URLs). New signals still land on the timeline and feed Plays; only the Inbox
    # re-alert is held back. 0 disables the cool-down.
    inbox_realert_cooldown_days: int = 7
    # Once an address is verified 'valid', re-verification of that email is allowed only after
    # this many days — a confirmed verdict doesn't decay faster, and repeat checks burn quota.
    email_reverify_cooldown_days: int = 30
    # Opt-in signal sources, comma separated. `rss` adds the company's own feed; `no_dorks`
    # removes the dork-backed search source (which is otherwise on, alongside the broad web query).
    # Default is the REAL sources (M16). It was "demo", which meant the out-of-the-box pipeline
    # emitted synthetic fixtures — so every alert, play and score built on it described events that
    # never happened. `demo` is now a test double: `demo_signals_active` keeps it out of
    # staging/prod, and `_reject_synthetic_signals_in_production` refuses to start rather than
    # disabling it silently. The offline suite injects DemoSignalSource explicitly and is unaffected.
    signal_sources: str = "web,rss"
    # How many dorks to run per account refresh, best first (see nexus/ingestion/dorks.py). Each
    # one is a billed search call, so this is the cost dial: 4 covers funding, hiring, exec change
    # and one corporate event. Raising it finds more and costs proportionally more.
    signal_dork_max_queries: int = 4
    # Seconds between dork queries. Keyless DuckDuckGo scrapes an HTML endpoint and 403s after
    # ~10 rapid requests, which one account refresh can reach alone; keyed engines are limited by
    # contract instead and need no spacing. Auto-selected per provider unless set.
    signal_dork_pace_s: float = 0.0
    # Per-tenant ceiling on billed source runs per UTC day. Automation is now on by default for new
    # workspaces, so this is what stops an enthusiastic account import from producing a surprise
    # bill: each source run is one row in `signal_source_runs`, so the crawl history IS the budget
    # ledger and no counter can drift from it. 0 disables the cap.
    #
    # 400/day at ~5 sources per account is roughly 80 account refreshes — comfortably above the
    # 6-hourly cycle for a few hundred accounts, and a hard stop well short of a runaway.
    tenant_daily_source_runs: int = 400
    # Shared company crawl fan-out (docs/superpowers/plans/2026-07-31-master-company-data-layer.md).
    # OFF until the diff harness reports agreement on real data: fan-out multiplies any attribution
    # mistake by the number of tenants subscribed to that company, and this subsystem has already
    # shipped six wrong-attribution bugs, four found only by running against live providers.
    # The shared crawl itself runs regardless — in shadow, consumed by nobody.
    shared_company_crawl_enabled: bool = True
    # Dedicated search backend for SIGNAL collection only. Empty (the default) means "use
    # `search_provider`", so behaviour is unchanged until an operator sets it.
    #
    # It exists because `search_provider` is global, and several features depend on capabilities
    # only Exa implements — `find_similar` (lookalikes) and `search_companies` (company discovery,
    # ICP auto-discovery). Repointing the global one at Firecrawl to diversify signal collection
    # would silently strip those: `find_similar` falls back to the base class and returns [], and
    # lookalikes degrade to "no results" with nothing in the logs to explain it. This slot keeps
    # the two decisions independent.
    signal_search_provider: str = ""
    # Web-search backend: duckduckgo|exa|brave|serper|firecrawl. Keyless DuckDuckGo is the
    # default; it 403s after ~10 rapid queries, so any real crawl volume needs a keyed engine.
    # `firecrawl` and `serper` are keyword/Google-backed (the operator dorks work as written and
    # `tbs` gives real recency); `exa` is neural (dorks switch to their phrase form automatically).
    search_provider: str = "duckduckgo"
    research_provider: str = "stub"            # account-research backend
    # Email-deliverability backend: "stub" (syntax only, offline default) | "dns" (free DNS/MX
    # domain check, no infra) | "reacher" (full SMTP mailbox probe, needs an off-host verifier).
    email_verify_provider: str = "stub"
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
    account_refresh_interval_s: int = 21600     # HOT accounts: staleness before re-processing (6h)
    # COLD accounts: nothing has happened here in a month, nobody has it in a cadence, and it is
    # on no list. Measured motivation: a uniform 6h cycle over 500 tenants x 1000 accounts demands
    # 23.15 accounts/sec against a measured 0.036/sec drain. Most of that is spent re-crawling
    # accounts where nothing has changed for months. See nexus/ingestion/tiering.py for the rules
    # and for why they are biased toward hot.
    account_refresh_interval_cold_s: int = 259200   # 72h
    # How far back a signal still counts as "this account is live". A month is long enough that a
    # quarterly-cadence company stays hot between events, and short enough that a genuinely dormant
    # account does eventually cool.
    account_hot_signal_window_days: int = 30
    account_refresh_batch_size: int = 100       # max accounts claimed per tick across tenants
    # Run an account's signal sources concurrently rather than one after another. Measured over 355
    # real crawls: sum of the five sources is 26.98s, the slowest single source is 14.94s — a 1.81x
    # cut in per-account wall time for no extra CPU, because the pipeline is ~99% await-on-network.
    # A kill switch rather than a rollout flag: if a provider turns out to rate-limit on
    # concurrency, this restores the old behaviour without a deploy.
    signal_sources_concurrent: bool = True

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
    # reps fabricated funding/hiring events as if they were real. This flag is honored in
    # local/test, but `demo_signals_active` HARD-DISABLES it in staging/prod regardless of
    # value — so a forgotten env var can never leak fabricated signals to a live tenant.
    demo_signals_enabled: bool = True

    # Daily digest (SDR adoption): once per interval, summarize the last day's GTM activity
    # into an email-channel alert per opted-in tenant. Rides the automation heartbeat (only
    # ticks while automation_enabled) and is idempotent per interval, so the scheduler can
    # enqueue it every tick without double-sending.
    digest_interval_hours: int = 24

    # Prometheus /metrics. ON by default since M15: a deployment that cannot see queue lag,
    # 402 rates, dunning depth or webhook failures is not operable, and "observability is opt-in"
    # in practice means "nobody turned it on".
    #
    # It was off because an unpinned build once pulled a FastAPI whose routers the instrumentator
    # could not introspect, and every endpoint 500ed. Two things changed: pyproject pins both
    # sides against exactly that pair, and `_maybe_enable_metrics` wraps instrumentation so an
    # incompatible install degrades to "no metrics" instead of breaking the app. Set false to opt
    # out; the app is unaffected either way.
    metrics_enabled: bool = True
    # The worker serves no HTTP, so it exports its own registry here — job outcomes and the state
    # gauges (queue depth, dead letters, dunning depth). Bound inside the compose network; never
    # published. 0 disables the listener while leaving metrics_enabled alone.
    worker_metrics_port: int = 9100
    worker_metrics_interval_s: float = 30.0

    # ---- Billing platform (commercial OS) --------------------------------------------------
    # Enforcement mode is the master kill switch (docs/billing/15-Migration-Strategy.md §1):
    #   off    = the seam is a no-op passthrough (incident escape hatch)
    #   shadow = evaluate + record usage, NEVER block  (safe default; ships dark)
    #   on     = evaluate + record + enforce per-capability
    billing_enforcement: Literal["off", "shadow", "on"] = "shadow"
    # Comma-separated emails bootstrapped as platform super-admins (chicken-and-egg solution for
    # the /admin portal). Empty = nobody has platform access until a row is inserted by hand.
    platform_admin_emails: str = ""
    # Sync the capability/plan seed into the DB on startup. Off in tests (fixtures seed directly).
    billing_seed_on_startup: bool = True

    # Relationship-graph network connectors (real OAuth + token encryption). Empty default →
    # the provider is inert (its /oauth/start returns 400) — never a fake-data fallback.
    network_google_client_id: str = ""
    network_google_client_secret: str = ""
    network_microsoft_client_id: str = ""
    network_microsoft_client_secret: str = ""
    network_microsoft_tenant: str = "common"   # Azure tenant id, or "common" (work + personal)
    # Base URL the OAuth provider redirects back to, e.g. https://app.example.com. The callback
    # path (/api/network/oauth/{provider}/callback) is appended; never client-supplied.
    network_oauth_redirect_base: str = ""
    # Fernet key (urlsafe-b64, 32 bytes) for encrypting stored OAuth tokens. Empty → derived
    # deterministically from secret_key, so tokens are always encrypted with no extra secret.
    network_token_enc_key: str = ""

    # Hosted web-search API keys, consumed only when `search_provider` selects that engine.
    # Secrets: set via NEXUS_*_API_KEY env (or a gitignored .env). NEVER commit a real value.
    # A selected engine with no key degrades to keyless DuckDuckGo so search keeps working.
    exa_api_key: str = ""
    # Optional rotation pool (comma-separated). The provider switches to the next key on a 429,
    # so bursty discovery rides out a single key's rate limit instead of going dark.
    exa_api_keys: str = ""
    brave_api_key: str = ""
    serper_api_key: str = ""
    # Firecrawl: Google-backed keyword search + page scraping. Inert until keyed. Added so the
    # signal pipeline is not dependent on any single vendor — keyless DuckDuckGo 403s under crawl
    # volume, and Exa is one company's neural index that does not honour the operator dorks.
    firecrawl_api_key: str = ""
    # Rotation pool, same semantics as `exa_api_keys`: comma-separated, primary first, tried in
    # turn when one key rate-limits. A single free-tier key is exhausted quickly by a crawl that
    # issues several queries per account.
    firecrawl_api_keys: str = ""
    # Phone enrichment is REP-TRIGGERED, never automatic. Each lookup is a paid actor run, so a
    # background sweep across a 1,000-contact workspace is a four-figure bill nobody asked for. The
    # rep clicks the contact they are about to call. Flipping this on enables a bulk/background
    # path; it stays off until someone decides that spend deliberately.
    phone_enrich_auto: bool = False

    # Contact details in the shared people store are Fernet-sealed at rest. Separate from
    # `mfa_secret_enc_key` and `network_token_enc_key` so the three rotate independently —
    # rotating the key that protects phone numbers must not orphan every MFA seed. Blank derives
    # one from `secret_key`, so the data is always encrypted with no extra configuration.
    people_data_enc_key: str = ""

    # External source databases (nexus/sources/). A registered DSN is a live credential to
    # somebody else's database, so it is Fernet-sealed at rest under its own key — rotating the
    # key that protects third-party database credentials must not orphan MFA seeds or network
    # OAuth tokens. Blank derives one from `secret_key`, so a DSN is always encrypted.
    source_db_dsn_enc_key: str = ""
    # Whether a source DSN may point at a private/loopback address. FALSE everywhere by default:
    # the SSRF guard in `sources/safety.py` is the whole reason a DSN form is safe to expose, and
    # the credential being typed in usually belongs to a customer rather than to the admin typing
    # it. This is the plan's open question, answered: it is a *setting* and never a request
    # parameter, so an admin cannot switch off the guard from the form the guard protects.
    # `_reject_private_source_dsn_in_production` below refuses to start if it is true in
    # staging/prod, mirroring how demo signal sources are handled.
    source_db_allow_private: bool = False
    # Ceiling on rows a dry run reads. A dry run exists to prove a mapping, not to move data; an
    # unbounded "sample" against a vendor's production table is a load test they did not agree to.
    source_db_dry_run_limit: int = 25
    # Per-statement timeout for anything we run against a source database. Someone else's
    # database is not something we can tune, and an introspection query that hangs would pin a
    # request worker for as long as they let it.
    source_db_statement_timeout_s: int = 10

    # Apify: actor marketplace, used for the lookups with no compliant public API (a phone number
    # behind a LinkedIn profile, a Crunchbase organisation page). Inert until keyed — an unkeyed
    # call raises rather than returning [], so a missing key is never read as "no results".
    apify_api_key: str = ""
    # Rotation pool, same semantics as `exa_api_keys` and `firecrawl_api_keys`. An actor run costs
    # more than a search call and free-tier credit is consumed quickly, so a pool matters more here.
    apify_api_keys: str = ""

    # Optional GitHub token. Unauthenticated GitHub allows 60 requests/hour — a handful of accounts
    # before the budget is gone (measured: 50 remaining after three calls). A token raises it to
    # 5,000. The source degrades to `throttled` rather than failing without one.
    github_token: str = ""

    # Signal -> alert (M21). ON by default: `signal.created` was published on every ingested signal
    # and nothing subscribed to it, so the whole collection pipeline landed in a table nobody was
    # notified about. The floor keeps weak mentions (the classifier's 0.4 tier) off the inbox — an
    # alert costs attention, and attention spent on a press mention is attention not spent on a
    # funding round. Set `signal_alerts_enabled=false` to restore the previous silence.
    signal_alerts_enabled: bool = True
    signal_alert_floor: float = 0.5

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
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

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

    @property
    def platform_admin_email_list(self) -> list[str]:
        return [e.lower() for e in self._csv_list(self.platform_admin_emails)]

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
    def demo_signals_active(self) -> bool:
        """Whether synthetic demo signals actually feed the pipeline.

        Fail-safe: hard-off in staging/prod so a live tenant can never be shown fabricated
        events, even if NEXUS_DEMO_SIGNALS_ENABLED was left true by mistake. Honors the raw
        flag only in local/test, where synthetic data is the point (offline demos, fixtures).
        """
        if self.env in ("staging", "prod"):
            return False
        return self.demo_signals_enabled

    @property
    def exa_api_key_list(self) -> list[str]:
        return self._key_pool(self.exa_api_key, self.exa_api_keys)

    @property
    def groq_api_key_list(self) -> list[str]:
        return self._key_pool(self.groq_api_key, self.groq_api_keys)

    @property
    def firecrawl_api_key_list(self) -> list[str]:
        return self._key_pool(self.firecrawl_api_key, self.firecrawl_api_keys)

    @property
    def apify_api_key_list(self) -> list[str]:
        return self._key_pool(self.apify_api_key, self.apify_api_keys)

    @model_validator(mode="after")
    def _reject_insecure_prod(self) -> "Settings":
        if self.env in ("staging", "prod") and self.secret_key == _INSECURE_SECRET:
            raise ValueError(
                "NEXUS_SECRET_KEY must be set to a strong value outside local/test"
            )
        return self

    @model_validator(mode="after")
    def _reject_synthetic_signals_in_production(self) -> "Settings":
        """Refuse to start a live deployment that asks for fabricated signals.

        ``demo_signals_active`` already forces synthetic signals off in staging/prod, but it does so
        **silently** — an operator who put ``demo`` in NEXUS_SIGNAL_SOURCES sees no demo signals, no
        error, and concludes signal collection is broken. Worse, the reverse reading is available
        too: someone can believe the pipeline is fed by a source that is not running.

        Failing at startup is the honest version. It costs one clear error message on a config that
        could never have worked, and it is the difference between "signals are fabricated" and
        "signals are real" being a checkable property of the deployment.
        """
        if self.env not in ("staging", "prod"):
            return self
        if "demo" in [t.strip().lower() for t in self.signal_sources.split(",")]:
            raise ValueError(
                "NEXUS_SIGNAL_SOURCES lists 'demo' but NEXUS_ENV is "
                f"'{self.env}'. Synthetic signals must never reach a live tenant; remove 'demo' "
                "(the real sources are 'web' and 'rss')."
            )
        return self

    @model_validator(mode="after")
    def _reject_private_source_dsn_in_production(self) -> "Settings":
        """Refuse to start a live deployment with the source-database SSRF guard switched off.

        ``source_db_allow_private`` exists because a source database genuinely does live on
        localhost in development, and refusing it there makes the feature untestable. In a live
        deployment it is the opposite: the guard is the only thing standing between a DSN form and
        a read oracle over our own container network and cloud metadata endpoint — and the
        credential typically belongs to a customer, so the admin typing it may not own what it
        points at.

        Same shape, and the same reasoning, as ``_reject_synthetic_signals_in_production``:
        silently ignoring the flag would leave an operator believing a guard is off when it is on,
        or (far worse) the reverse. One clear error at startup on a configuration that must never
        have shipped.
        """
        if self.env in ("staging", "prod") and self.source_db_allow_private:
            raise ValueError(
                "NEXUS_SOURCE_DB_ALLOW_PRIVATE is true but NEXUS_ENV is "
                f"'{self.env}'. That disables the SSRF guard on admin-supplied connection "
                "strings; it is a local-development affordance only."
            )
        return self

    @model_validator(mode="after")
    def _validate_token_enc_key(self) -> "Settings":
        key = (self.network_token_enc_key or "").strip()
        if key:
            from cryptography.fernet import Fernet

            try:
                Fernet(key.encode())
            except Exception as exc:  # malformed key must fail loudly at startup, not silently at runtime
                raise ValueError(
                    "NEXUS_NETWORK_TOKEN_ENC_KEY must be a valid urlsafe-base64 32-byte Fernet key"
                ) from exc
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
