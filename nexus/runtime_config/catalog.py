# nexus/runtime_config/catalog.py
"""Which settings a superadmin may change at runtime, and what happens when they do.

**This is an allowlist, and the exclusions carry more weight than the inclusions.** A setting is
reachable from the panel only if it appears here. Everything else — 174 fields on ``Settings`` —
stays where it is, changeable by deploy alone.

Three questions decide whether a setting belongs:

1. *Does changing it at runtime do anything?* Startup-only wiring (database URLs, whether to seed
   the billing catalog) is inert after boot, so a control for it would be a lie.
2. *Is it safe to change from a web form?* Several settings are guards, and a guard that can be
   switched off from the interface it protects is not a guard.
3. *Can the operator predict the consequence?* Every entry carries an ``effect`` and, where the
   change costs money or turns something off, a ``warning``. A toggle whose result nobody can state
   in a sentence is a trap, not a feature.

Deliberately excluded, with reasons, because "why can't I change X here?" is a question that will
be asked:

* ``source_db_allow_private`` — the SSRF guard on external source databases. An admin must not be
  able to switch off the guard from the form the guard protects. It is a local-dev setting, and
  ``Settings._reject_private_source_dsn_in_production`` already refuses to boot with it on.
* ``security_headers_enabled`` — turning off CSP and friends from a web page is a self-inflicted
  vulnerability, executed through the exact surface it defends.
* ``auth_rate_limit_enabled`` — brute-force protection on the login path. Same argument.
* ``demo_signals_enabled`` — fabricated signals reaching a real inbox is the failure
  ``nexus/ingestion`` was rebuilt to prevent. It is hard-false in staging and prod, and a runtime
  toggle would reintroduce exactly the state the two existing guards exist to make impossible.
* ``billing_seed_on_startup`` — startup-only; flipping it after boot does nothing.
* ``env``, ``secret_key``, database and Redis URLs, every ``*_enc_key`` — identity and cryptographic
  roots. See ``providers/catalog.py`` for the same rule applied to keys.

``requires_restart`` marks settings that are read once into a module-level object. The panel still
offers them, because knowing the new value is stored and pending is better than not being able to
set it at all — but it says so rather than implying an effect that will not arrive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str                    # the Settings field name
    label: str                  # what to call it on screen
    group: str
    kind: Literal["bool", "int", "float", "str"]
    effect: str                 # what changing it does, in one sentence
    warning: str = ""           # what it costs or breaks. Empty when there is nothing to warn about
    risk: Risk = "low"
    requires_restart: bool = False
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = field(default_factory=tuple)


_SPECS: tuple[SettingSpec, ...] = (
    # ---- automation: the things that spend money on their own -----------------------------------
    SettingSpec(
        key="automation_enabled", label="Autonomous heartbeat", group="Automation", kind="bool",
        effect="Runs the recurring GTM loop on a timer: account refresh and cadence advance, "
               "without anyone clicking anything.",
        warning="This is the switch that makes the platform spend money unattended. Every account "
                "refresh is a crawl, and crawls are the largest line in COGS. A workspace also "
                "needs its own automation_enabled before anything happens to it.",
        risk="high",
    ),
    SettingSpec(
        key="icp_discovery_enabled", label="Daily ICP discovery", group="Automation", kind="bool",
        effect="Each interval, finds net-new companies and adds the ones that strictly match a "
               "workspace's ICP.",
        warning="Rides the heartbeat above, so it does nothing until that is on. Adds accounts, "
                "which are then refreshed on a schedule — the cost compounds rather than being "
                "one-off. A workspace with an empty ICP discovers nothing, silently.",
        risk="high",
    ),
    SettingSpec(
        key="cadence_enabled", label="Email cadences", group="Automation", kind="bool",
        effect="Turns on the multi-touch cadence engine. The advance tick is a no-op until this "
               "is set.",
        warning="Cadence steps send real email to real prospects. Check the sending domain and the "
                "drafted copy before enabling.",
        risk="high",
    ),
    SettingSpec(
        key="campaign_sourcing_enabled", label="Contact sourcing", group="Automation", kind="bool",
        effect="Enables net-new contact providers and the verifying email finder.",
        warning="Each sourced contact is a paid lookup plus a verification. Off means the stub, "
                "which returns nothing rather than costing anything.",
        risk="medium",
    ),
    SettingSpec(
        key="account_enrich_enabled", label="Fill blank firmographics", group="Automation",
        kind="bool",
        effect="Fills missing industry, size, country and tech-stack from the web when no premium "
               "data provider is configured.",
        warning="Only touches accounts that are missing those fields, so the cost is bounded by "
                "how incomplete the data is — not by how many accounts exist.",
        risk="medium",
    ),
    SettingSpec(
        key="phone_enrich_auto", label="Bulk phone lookup", group="Automation", kind="bool",
        effect="Allows phone enrichment to run in the background instead of only when a rep opens "
               "a contact.",
        warning="A background sweep of a 1,000-contact workspace is a four-figure bill nobody "
                "asked for. Phone lookups are among the most expensive calls we make. Leave off "
                "unless that spend is a decision someone has taken.",
        risk="high",
    ),
    SettingSpec(
        key="crm_sync_enabled", label="Push to CRM", group="Automation", kind="bool",
        effect="Pushes changed accounts out to each workspace's connected CRM.",
        warning="Writes into the customer's own CRM. Change-aware, so only stale or modified "
                "accounts move — but a mapping mistake reaches their production records.",
        risk="high",
    ),

    # ---- collection and alerting ----------------------------------------------------------------
    SettingSpec(
        key="signal_alerts_enabled", label="Alert on new signals", group="Signals", kind="bool",
        effect="Creates an alert when an ingested signal clears the strength floor.",
        warning="Turning this off restores the previous silence: signals are still collected and "
                "stored, and nobody is told about them. That was the bug this wire fixed — a rep "
                "learning about a customer's funding round by scrolling.",
        risk="medium",
    ),
    SettingSpec(
        key="signal_sources_concurrent", label="Run sources concurrently", group="Signals",
        kind="bool",
        effect="Fetches a signal source's network calls in parallel. Measured at 1.81x faster per "
               "account crawl.",
        warning="A kill switch, not a rollout flag. Turn it off only if a provider starts "
                "rate-limiting on concurrency; it restores the old sequential behaviour without a "
                "deploy.",
        risk="low",
    ),
    SettingSpec(
        key="shared_company_crawl_enabled", label="Shared company crawl fan-out",
        group="Signals", kind="bool",
        effect="Delivers signals found by the shared cross-tenant crawl into individual "
               "workspaces, instead of each crawling the same company separately.",
        warning="Each company is still gated individually on a recorded agreement between the "
                "shared and per-tenant crawls, so switching this on changes nothing until that "
                "evidence exists. Turning it off returns everyone to per-tenant crawling, which "
                "is correct but more expensive.",
        risk="medium",
    ),
    SettingSpec(
        key="signal_alert_floor", label="Alert strength floor", group="Signals", kind="float",
        effect="Minimum signal strength that becomes an alert. 0.5 by default.",
        warning="Lowering it toward 0.4 lets weak press mentions onto the inbox. An alert costs "
                "attention, and attention spent on a mention is attention not spent on a funding "
                "round.",
        risk="medium", minimum=0.0, maximum=1.0,
    ),

    # ---- money ----------------------------------------------------------------------------------
    SettingSpec(
        key="billing_enforcement", label="Billing enforcement", group="Billing", kind="str",
        effect="off = no metering at all. shadow = evaluate and record every decision but never "
               "block. on = quotas and plans are enforced against customers.",
        warning="Moving from shadow to on is the moment plan limits become real. Anything shadow "
                "mode has been recording as 'would block' starts returning 402 to a paying "
                "customer. Read the would_block counter before flipping this.",
        risk="high", options=("off", "shadow", "on"),
    ),
    SettingSpec(
        key="billing_dunning_enabled", label="Dunning retries", group="Billing", kind="bool",
        effect="Retries failed collections on a schedule and escalates to past-due.",
        warning="Turning it off stops chasing failed payments. The debt stays visible and is never "
                "voided, but nothing will try to collect it again.",
        risk="medium",
    ),
    SettingSpec(
        key="job_retry_enabled", label="Job retries", group="Reliability", kind="bool",
        effect="Retries a failed background job with exponential backoff before parking it.",
        warning="Off degrades to fail-fast: the job is still dead-lettered with its payload "
                "intact, so evidence is kept and work is never silently lost. It just will not be "
                "attempted again automatically.",
        risk="medium",
    ),
    SettingSpec(
        key="idempotency_enabled", label="Idempotency keys", group="Reliability", kind="bool",
        effect="De-duplicates requests carrying an Idempotency-Key header; a retry replays the "
               "first response instead of re-running the work.",
        warning="Needs Redis for cross-worker de-duplication. With the in-process backend and more "
                "than one API replica, two replicas will not see each other's keys.",
        risk="low",
    ),
    SettingSpec(
        key="metrics_enabled", label="Prometheus metrics", group="Reliability", kind="bool",
        effect="Serves /metrics for scraping.",
        warning="Turning this off makes the deployment blind to queue lag, 402 rates and dunning "
                "depth. It exists as a switch because an unpinned build once broke every endpoint "
                "through the instrumentator — not because running without it is normal.",
        risk="medium", requires_restart=True,
    ),

    # ---- search cost -----------------------------------------------------------------------------
    #
    # Why these are per-task rather than one global switch. `search_provider` drives every plain
    # `.search()` in the product, and those tasks have wildly different value per query. Measured on
    # the live deployment: account enrichment alone was 123 of the billed search events across 56
    # accounts, all on Exa.
    #
    # `find_similar` (lookalike accounts) and `search_companies` (ICP/company discovery) are the
    # ONLY capabilities that genuinely need Exa -- every other provider returns [] for them, so
    # those features go dark elsewhere. Everything else is a plain query that any index answers, so
    # pointing the bulk work at a cheaper one costs nothing in capability.
    #
    # Empty means "use `search_provider`", so a deployment that sets none behaves exactly as before.
    SettingSpec(
        key="enrichment_search_provider", label="Enrichment search provider",
        group="Search cost", kind="str",
        effect="Which index answers account-firmographic lookups. This is the highest-volume "
               "search in the product, so it is where a provider change shows up on the bill.",
        warning="Leave empty to follow the global provider. Exa is not required here -- enrichment "
                "issues a plain query, and paying Exa rates for it is the single largest avoidable "
                "cost measured on this deployment.",
        risk="low", options=("", "firecrawl", "brave", "serper", "exa", "duckduckgo"),
    ),
    SettingSpec(
        key="contact_search_provider", label="Contact discovery search provider",
        group="Search cost", kind="str",
        effect="Which index is queried when finding real named people at an account.",
        warning="Recall matters more here than cost: a missed contact is a rep with nobody to "
                "call. Exa's semantic matching is genuinely better at people queries, which is why "
                "this is worth keeping separate from enrichment rather than sharing one setting.",
        risk="low", options=("", "exa", "firecrawl", "brave", "serper", "duckduckgo"),
    ),
    SettingSpec(
        key="website_icp_search_provider", label="Website analysis search provider",
        group="Search cost", kind="str",
        effect="Which index gathers the pages used to draft an ICP from a company website.",
        warning="Two queries per run, and it runs when a user asks rather than on a schedule -- so "
                "this is a low-volume path where provider choice barely affects the bill.",
        risk="low", options=("", "firecrawl", "brave", "serper", "exa", "duckduckgo"),
    ),
    SettingSpec(
        key="discovery_search_provider", label="Discovery sweep search provider",
        group="Search cost", kind="str",
        effect="Which index answers the plain-query fallback in net-new account sweeps.",
        warning="This does NOT cover `search_companies`, which only Exa implements -- ICP discovery "
                "still uses Exa for that call whatever this is set to, and setting a non-Exa "
                "provider here changes only the fallback path.",
        risk="low", options=("", "firecrawl", "brave", "serper", "exa", "duckduckgo"),
    ),
    SettingSpec(
        key="account_enrich_min_interval_days", label="Enrichment re-attempt interval (days)",
        group="Search cost", kind="int", minimum=0, maximum=365,
        effect="How long to wait before re-attempting enrichment on an account. A person pressing "
               "Enrich is never throttled by this.",
        warning="0 disables the backoff and restores the old behaviour: an account the web has no "
                "firmographics for will then issue a search request and an LLM completion on every "
                "refresh cycle, forever, buying nothing each time.",
        risk="low",
    ),

    # ---- access ---------------------------------------------------------------------------------
    SettingSpec(
        key="otp_registration_enabled", label="Two-step registration", group="Access", kind="bool",
        effect="New sign-ups verify an emailed code before the account is created.",
        warning="Needs working outbound email. With email misconfigured, nobody can register at "
                "all — and the failure looks like a broken sign-up form rather than a mail problem.",
        risk="high",
    ),
    SettingSpec(
        key="admin_ip_allowlist", label="Control plane IP allowlist", group="Access", kind="str",
        effect="Comma-separated IP addresses or CIDR ranges that may reach this Control plane. "
               "Empty means any address.",
        warning="At most two entries — use a CIDR range for an office. Get this wrong and you lock "
                "yourself out of the panel you would use to fix it, so read the address in the "
                "refusal message: behind a proxy it is often not the one you expect. A malformed "
                "list is ignored rather than enforced, which is the deliberate escape hatch.",
        risk="high",
    ),
    SettingSpec(
        key="calling_enabled", label="Calling module", group="Access", kind="bool",
        effect="Enables the call console, call queue and dispositions.",
        warning="Click-to-dial works without a telephony provider. Turning this off hides the "
                "feature from every workspace regardless of plan.",
        risk="medium",
    ),
)

CATALOG: dict[str, SettingSpec] = {s.key: s for s in _SPECS}

# Named, not merely absent, so the reason survives. `test_the_forbidden_settings_are_never_settable`
# asserts none of these can reach the catalog.
FORBIDDEN: frozenset[str] = frozenset({
    "source_db_allow_private",
    "security_headers_enabled",
    "auth_rate_limit_enabled",
    "demo_signals_enabled",
    "billing_seed_on_startup",
    "env",
    "secret_key",
    "database_url",
    "redis_url",
    "network_token_enc_key",
    "mfa_secret_enc_key",
    "source_db_dsn_enc_key",
    "stripe_secret_key",
    "stripe_webhook_secret",
})


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Bring a JSON value to the type the setting expects, and refuse what does not fit.

    The panel posts JSON, so a boolean can arrive as the string "false" — which is truthy in
    Python and would switch a guard ON while the operator watched it read "off".
    """
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return raw.strip().lower() in ("true", "1", "yes")
        raise ValueError(f"{spec.key} is a switch; expected true or false, got {raw!r}")
    if spec.kind in ("int", "float"):
        try:
            value = int(raw) if spec.kind == "int" else float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.key} expects a number, got {raw!r}") from exc
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"{spec.key} cannot be below {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"{spec.key} cannot be above {spec.maximum}")
        return value
    value = str(raw)
    if spec.options and value not in spec.options:
        raise ValueError(f"{spec.key} must be one of {spec.options}")
    return value
