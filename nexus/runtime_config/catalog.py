# nexus/runtime_config/catalog.py
"""Which settings a superadmin may change at runtime, and what happens when they do.

**This is an allowlist, and the exclusions carry more weight than the inclusions.** A setting is
reachable from the panel only if it appears here. Everything else on ``Settings`` stays where it is,
changeable by deploy alone.

Three questions decide whether a setting belongs:

1. *Does changing it at runtime do anything?* Startup-only wiring (database URLs, whether to seed
   the billing catalog) is inert after boot, so a control for it would be a lie. So is a value
   copied once into a long-lived object: it needs an on-change hook in ``service.py`` or a reader
   that looks it up per call (``tests/test_runtime_control_plane.py`` checks every key is read).
2. *Is it safe to change from a web form?* Several settings are guards, and a guard that can be
   switched off from the interface it protects is not a guard. Free text is validated before it is
   stored, because ``coerce`` checks only the declared kind.
3. *Can the operator predict the consequence?* Every entry carries an ``effect`` and, where the
   change costs money or turns something off, a ``warning``. A toggle whose result nobody can state
   in a sentence is a trap, not a feature.

Deliberately excluded, with reasons, because "why can't I change X here?" is a question that will
be asked:

* ``source_db_allow_private`` and ``alert_webhook_allow_private``: SSRF guards. An admin must not be
  able to switch off the guard from the form the guard protects.
* ``security_headers_enabled``, ``auth_rate_limit_enabled``: the same argument aimed at the web
  surface through the exact interface it defends.
* ``demo_signals_enabled``: fabricated signals reaching a real inbox is the failure
  ``nexus/ingestion`` was rebuilt to prevent.
* ``billing_seed_on_startup``: startup-only; flipping it after boot does nothing.
* ``env``, ``secret_key``, database and Redis URLs, every ``*_enc_key``: identity and cryptographic
  roots. See ``providers/catalog.py`` for the same rule applied to keys.
* ``email_verify_auth_header``: a credential, and the panel returns every value in plaintext.
* ``billing_support_credit_cap``: it widens what another platform permission can grant, which is a
  permissions decision rather than a runtime setting.
* ``personalization_posts_window``: passed verbatim to the Apify posts actor, whose accepted values
  nobody has observed.
* ``cadence_tick_interval_s``: nothing reads it.
* ``signal_sources``: read once into the ingestion service singleton, which the test suite injects
  its demo source through, so there is no safe rebuild hook for it.
* ``phone_enrich_auto`` and ``calling_enabled``: REMOVED 2026-09-15, found by the reader-coverage
  test. Nothing reads either, so the "Bulk phone lookup" and "Calling module" switches saved,
  reported "in effect" and changed nothing. There is no background phone path to allow, and the
  calling module is gated by the ``module.calling`` capability, which Feature switches turns off.
  A stored row for either key is skipped, so removing them is safe for a deployment that set one.

``requires_restart`` marks settings that are read once into a module-level object. The panel still
offers them, because knowing the new value is stored and pending is better than not being able to
set it at all, but it says so rather than implying an effect that will not arrive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str                    # the Settings field name
    label: str                  # what to call it on screen
    group: str                  # one of GROUP_ORDER
    kind: Literal["bool", "int", "float", "str"]
    effect: str                 # what changing it does, in one sentence
    warning: str = ""           # what it costs or breaks. Empty when there is nothing to warn about
    risk: Risk = "low"
    requires_restart: bool = False
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = field(default_factory=tuple)
    # What each option is called on screen. The stored value stays the raw option.
    option_labels: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    placeholder: str = ""


EMAIL = "Email finding & verification"
CONTACTS = "Contacts & enrichment"
PERSONALIZATION = "Personalization"
AI = "AI & research"
SIGNALS = "Signals & alerts"
AUTOMATION = "Automation & schedules"
OUTREACH = "Outreach & CRM"
BILLING = "Billing"
ACCESS = "Access & security"
RELIABILITY = "Reliability"

#: The order the panel shows groups in. Server-defined so a new group lands deliberately rather
#: than wherever the alphabet puts it; `test_every_group_is_one_the_panel_orders_and_none_is_empty`
#: keeps the two in step.
GROUP_ORDER: tuple[str, ...] = (
    EMAIL, CONTACTS, PERSONALIZATION, AI, SIGNALS, AUTOMATION, OUTREACH, BILLING, ACCESS,
    RELIABILITY,
)


# Declaration order is the reading order inside a group: the switch or picker that decides whether
# anything happens first, then the dials that tune it.
_SPECS: tuple[SettingSpec, ...] = (
    # ---- email finding and verification ---------------------------------------------------------
    SettingSpec(
        key="email_verify_provider", label="Email verification", group=EMAIL, kind="str",
        effect="How found addresses are checked. 'reacher' asks the receiving mail server about the "
               "mailbox; 'dns' only checks the domain can receive mail; 'reacher,dns' tries Reacher "
               "and falls back to DNS when it is unreachable.",
        warning="'dns' alone never confirms a mailbox, so no address can ever read valid. Reacher "
                "needs the verifier URL below pointing at a running instance on a SEPARATE host, "
                "because probing from the app's own IP gets it blocklisted.",
        risk="medium", options=("reacher,dns", "reacher", "dns"),
        option_labels=(
            ("reacher,dns", "Reacher, DNS when unreachable"),
            ("reacher", "Reacher only"),
            ("dns", "DNS only (no mailbox check)"),
        ),
    ),
    SettingSpec(
        key="email_verify_url", label="Reacher verifier URL", group=EMAIL, kind="str",
        effect="Where Reacher's /v0/check_email endpoint answers. Every address check is a POST "
               "to this URL.",
        warning="A wrong or unreachable URL does not fail loudly: every check falls back to DNS "
                "(risky) or reads unknown. Press Check connection after saving. The host must be "
                "reachable from this deployment, so a raw :8080 port blocked by a cloud firewall "
                "looks exactly like a dead verifier.",
        risk="high", placeholder="https://verifier.example.com/v0/check_email",
    ),
    SettingSpec(
        key="email_verify_timeout_s", label="Verifier timeout (seconds)", group=EMAIL,
        kind="float", minimum=2, maximum=60,
        effect="How long one address check may take before it is treated as unknown.",
        warning="SMTP servers are slow to answer, so a short timeout turns real verdicts into "
                "unknown. A long one holds up the email finder, which checks up to ten patterns "
                "per person.",
        risk="medium",
    ),
    SettingSpec(
        key="email_finder_max_candidates", label="Patterns tried per person", group=EMAIL,
        kind="int", minimum=1, maximum=20,
        effect="How many address patterns the email finder may check per contact (first.last, "
               "then first, and so on), stopping at the first valid one.",
        warning="Each pattern checked is one verification call. There are ten patterns, so a cap "
                "below ten stops before the later ones are ever tried.",
        risk="medium",
    ),
    SettingSpec(
        key="email_reverify_cooldown_days", label="Re-check a valid address after (days)",
        group=EMAIL, kind="int", minimum=0, maximum=365,
        effect="How long a confirmed-valid address is left alone by bulk re-verification and by "
               "Enrich. The Re-verify button on a contact ignores it.",
        warning="0 re-checks every valid address on every pass, spending a verification each time.",
    ),

    # ---- contacts and enrichment ----------------------------------------------------------------
    SettingSpec(
        key="contact_search_sources", label="Contact discovery source", group=CONTACTS,
        kind="str",
        effect="Where \"Find contacts\" looks for people. 'search' queries Exa and keeps only people "
               "proven to work at the account (its own site, or LinkedIn naming it).",
        warning="'stub' is the offline test double and the SHIPPED DEFAULT: it returns role "
                "placeholders that sourcing discards, so Find contacts finds nobody on any account. "
                "If a deployment 'finds no contacts', check this first.",
        risk="high", options=("search", "stub"),
        option_labels=(("search", "Exa search"), ("stub", "Off (test double)")),
    ),
    SettingSpec(
        key="campaign_sourcing_enabled", label="Contact sourcing", group=CONTACTS, kind="bool",
        effect="Enables net-new contact providers and the verifying email finder.",
        warning="Each sourced contact is a paid lookup plus a verification. Off means the stub, "
                "which returns nothing rather than costing anything.",
        risk="medium",
    ),
    SettingSpec(
        key="account_enrich_enabled", label="Fill blank firmographics", group=CONTACTS,
        kind="bool",
        effect="Fills missing industry, size, country and tech-stack from the web when no premium "
               "data provider is configured.",
        warning="Only touches accounts that are missing those fields, so the cost is bounded by "
                "how incomplete the data is, not by how many accounts exist.",
        risk="medium",
    ),
    SettingSpec(
        key="account_enrich_min_interval_days", label="Enrichment re-attempt interval (days)",
        group=CONTACTS, kind="int", minimum=0, maximum=365,
        effect="How long to wait before re-attempting enrichment on an account. A person pressing "
               "Enrich is never throttled by this.",
        warning="0 disables the backoff and restores the old behaviour: an account the web has no "
                "firmographics for will then issue a search request and an LLM completion on every "
                "refresh cycle, forever, buying nothing each time.",
    ),
    SettingSpec(
        key="lookalike_enrich_max", label="Look-alike candidates enriched", group=CONTACTS,
        kind="int", minimum=0, maximum=50,
        effect="How many look-alike candidates are enriched per search so they rank on real "
               "firmographics rather than search snippets.",
        warning="Each is a search plus an LLM call inside a request someone is waiting for, and the "
                "pass is capped at about 20 seconds, so raising this mostly adds candidates that "
                "time out. 0 ranks on snippets alone.",
        risk="medium",
    ),
    SettingSpec(
        key="phone_lookup_provider", label="Phone lookup", group=CONTACTS, kind="str",
        effect="Where a rep's phone lookup goes after the shared person record and any source "
               "database. Apify runs the paid phone_finder actor; Off buys nothing.",
        warning="Off stops new numbers being found. Numbers already bought still come back, and "
                "the lookup tells the rep it is turned off here. Apify needs the phone_finder actor "
                "approved on the Apify account; Platform health shows whether it is.",
        risk="medium", options=("apify", "off"),
        option_labels=(("apify", "Apify phone finder"), ("off", "Off")),
    ),

    # ---- personalization --------------------------------------------------------------------------
    SettingSpec(
        key="personalization_provider", label="Person-level personalization",
        group=PERSONALIZATION, kind="str",
        effect="Which provider fetches a contact's LinkedIn headline and About section, folded "
               "into every email draft and call script. Off fetches nothing and messages stay "
               "personalised on role and signals alone.",
        warning="Apify spends a paid actor run per contact enriched, on a path that runs in the "
                "background. It lives here so it can be switched off during an incident without a "
                "redeploy; the worker picks the change up within 30s.",
        risk="high", options=("apify", "stub"),
        option_labels=(("apify", "Apify (LinkedIn profile)"), ("stub", "Off")),
    ),
    SettingSpec(
        key="personalization_posts_enabled", label="Fetch recent LinkedIn posts",
        group=PERSONALIZATION, kind="bool",
        effect="Also fetches the contact's own recent posts, so a message can reference what they "
               "actually said. Headline and About are fetched either way.",
        warning="A SECOND paid actor run per contact, on top of the profile fetch: no actor "
                "returns both, so this roughly doubles the per-person cost. Off is a complete "
                "state: messages stay personalised on headline, About, role and signals.",
        risk="high",
    ),
    SettingSpec(
        key="personalization_max_posts", label="Posts referenced per contact",
        group=PERSONALIZATION, kind="int", minimum=1, maximum=10,
        effect="How many of a contact's recent posts are fetched and offered to a draft or call "
               "script.",
        warning="Also the number the posts actor is asked to return, so every extra post is paid "
                "for per contact. Only matters while recent posts are turned on.",
        risk="medium",
    ),

    # ---- AI and research ------------------------------------------------------------------------
    SettingSpec(
        key="llm_provider", label="LLM provider", group=AI, kind="str",
        effect="Which model provider writes every email draft, call script, research brief and "
               "summary.",
        warning="Leave on 'auto' unless you have a reason. 'auto' is the ONLY choice that uses Groq "
                "keys added under Provider keys; 'groq', 'anthropic' and 'openai_compat' need their "
                "key in the deployment environment, and without it every completion falls through "
                "to the offline stub, whose fluent, canned text is then sent to real prospects "
                "with nothing reporting a problem.",
        risk="high", options=("auto", "anthropic", "groq", "openai_compat"),
    ),
    SettingSpec(
        key="research_provider", label="Research brief source", group=AI, kind="str",
        effect="Where the account research brief and \"Ask about this account\" get live facts. "
               "'search' runs an Exa search and summarises what it finds.",
        warning="'stub' turns web research off: briefs and answers are written from stored data "
                "only, and read just as confidently. It is the shipped default.",
        risk="medium", options=("search", "stub"),
        option_labels=(("search", "Exa search"), ("stub", "Off (stored data only)")),
    ),

    # ---- signals and alerts ---------------------------------------------------------------------
    SettingSpec(
        key="signal_search_provider", label="Signal search provider", group=SIGNALS,
        kind="str",
        effect="Which index the signal dork search queries for funding, hiring and launch news. "
               "Never Exa: empty means Firecrawl. News, RSS, job boards and EDGAR are unaffected.",
        warning="A provider without a working key degrades to keyless DuckDuckGo, which is paced "
                "and starts refusing after about ten rapid queries: signals keep coming from the "
                "other sources but lose the dork search, their strongest one. Check this provider's "
                "key under Provider keys before switching; a 402 there means out of credits.",
        risk="medium", options=("", "firecrawl", "serper", "brave", "duckduckgo"),
    ),
    SettingSpec(
        key="signal_sources_concurrent", label="Run sources concurrently", group=SIGNALS,
        kind="bool",
        effect="Fetches a signal source's network calls in parallel. Measured at 1.81x faster per "
               "account crawl.",
        warning="A kill switch, not a rollout flag. Turn it off only if a provider starts "
                "rate-limiting on concurrency; it restores the old sequential behaviour without a "
                "deploy.",
    ),
    SettingSpec(
        key="shared_company_crawl_enabled", label="Shared company crawl fan-out",
        group=SIGNALS, kind="bool",
        effect="Delivers signals found by the shared cross-tenant crawl into individual "
               "workspaces, instead of each crawling the same company separately.",
        warning="Each company is still gated individually on a recorded agreement between the "
                "shared and per-tenant crawls, so switching this on changes nothing until that "
                "evidence exists. Turning it off returns everyone to per-tenant crawling, which "
                "is correct but more expensive.",
        risk="medium",
    ),
    SettingSpec(
        key="signal_alerts_enabled", label="Alert on new signals", group=SIGNALS, kind="bool",
        effect="Creates an alert when an ingested signal clears the strength floor.",
        warning="Turning this off restores the previous silence: signals are still collected and "
                "stored, and nobody is told about them. That was the bug this wire fixed, a rep "
                "learning about a customer's funding round by scrolling.",
        risk="medium",
    ),
    SettingSpec(
        key="signal_alert_floor", label="Alert strength floor", group=SIGNALS, kind="float",
        effect="Minimum signal strength that becomes an alert. 0.5 by default.",
        warning="Lowering it toward 0.4 lets weak press mentions onto the inbox. An alert costs "
                "attention, and attention spent on a mention is attention not spent on a funding "
                "round.",
        risk="medium", minimum=0.0, maximum=1.0,
    ),
    SettingSpec(
        key="inbox_min_signal_strength", label="Inbox task strength floor", group=SIGNALS,
        kind="float", minimum=0, maximum=1,
        effect="Minimum signal strength that opens an Inbox task for a rep. Weaker signals still "
               "land on the account's timeline.",
        warning="Lower puts weak mentions into every rep's Inbox; higher can keep real hiring and "
                "news signals out of it.",
        risk="medium",
    ),
    SettingSpec(
        key="inbox_realert_cooldown_days", label="Re-alert cool-down (days)", group=SIGNALS,
        kind="int", minimum=0, maximum=90,
        effect="After a rep completes an account's task, how many days before a new signal can "
               "open another one for it.",
        warning="0 turns the cool-down off, so the same news re-covered under fresh URLs reopens "
                "tasks the rep already dealt with.",
    ),
    SettingSpec(
        key="signal_dork_max_queries", label="Targeted searches per refresh", group=SIGNALS,
        kind="int", minimum=0, maximum=10,
        effect="How many targeted searches (funding, hiring, exec change, events) each account "
               "refresh runs, best first.",
        warning="Each one is a billed search call on every refresh, so this multiplies signal cost "
                "directly. 0 turns the targeted search off and signals lose their strongest source.",
        risk="medium",
    ),
    SettingSpec(
        key="tenant_daily_source_runs", label="Source runs per workspace per day", group=SIGNALS,
        kind="int", minimum=0, maximum=100000,
        effect="Most signal source runs one workspace may use per UTC day. Past it, refreshes skip "
               "the crawl until midnight UTC.",
        warning="A spend guard: set too low, a large workspace stops receiving signals partway "
                "through the day with only a log line saying why. 0 removes the cap.",
        risk="medium",
    ),
    SettingSpec(
        key="digest_interval_hours", label="Digest interval (hours)", group=SIGNALS, kind="int",
        minimum=1, maximum=168,
        effect="How often the signal digest is sent.",
    ),

    # ---- automation and schedules: the things that spend money on their own ----------------------
    SettingSpec(
        key="automation_enabled", label="Autonomous heartbeat", group=AUTOMATION, kind="bool",
        effect="Runs the recurring GTM loop on a timer: account refresh and cadence advance, "
               "without anyone clicking anything.",
        warning="This is the switch that makes the platform spend money unattended. Every account "
                "refresh is a crawl, and crawls are the largest line in COGS. A workspace also "
                "needs its own automation_enabled before anything happens to it.",
        risk="high",
    ),
    SettingSpec(
        key="automation_tick_interval_s", label="Heartbeat tick (seconds)", group=AUTOMATION,
        kind="int", minimum=15, maximum=3600,
        effect="Seconds between heartbeat ticks. Each tick queues the refresh, cadence, discovery "
               "and CRM sweeps that are due.",
        warning="A shorter tick does not refresh anything sooner than its own interval; it mostly "
                "adds queue churn. A longer one delays every scheduled job by up to one tick. Takes "
                "effect after the current wait.",
        risk="medium",
    ),
    SettingSpec(
        key="account_refresh_interval_s", label="Active account refresh (seconds)",
        group=AUTOMATION, kind="int", minimum=3600, maximum=604800,
        effect="How often an active (hot) account is re-crawled for signals. 21600, six hours, by "
               "default.",
        warning="The largest line in COGS: halving it roughly doubles crawl spend on every active "
                "account. Lengthening it means a funding round reaches the rep later.",
        risk="high",
    ),
    SettingSpec(
        key="account_refresh_interval_cold_s", label="Quiet account refresh (seconds)",
        group=AUTOMATION, kind="int", minimum=3600, maximum=2592000,
        effect="How often a quiet (cold) account is re-crawled. 259200, three days, by default.",
        warning="Quiet accounts are most of the estate, so shortening this is where crawl spend "
                "grows fastest.",
        risk="medium",
    ),
    SettingSpec(
        key="account_hot_signal_window_days", label="Active if a signal in (days)",
        group=AUTOMATION, kind="int", minimum=1, maximum=365,
        effect="An account with a signal inside this many days counts as active and is refreshed "
               "on the active interval.",
        warning="Wider makes more accounts active, so more crawls run. Narrower moves accounts to "
                "the quiet cycle sooner, so their next signal arrives later.",
        risk="medium",
    ),
    SettingSpec(
        key="account_refresh_batch_size", label="Accounts claimed per tick", group=AUTOMATION,
        kind="int", minimum=1, maximum=1000,
        effect="Most accounts claimed for refresh per heartbeat tick, across all workspaces.",
        warning="Higher drains a backlog faster but queues more crawls at once against the same "
                "worker and search budgets.",
        risk="medium",
    ),
    SettingSpec(
        key="icp_discovery_enabled", label="Daily ICP discovery", group=AUTOMATION, kind="bool",
        effect="Each interval, finds net-new companies and adds the ones that strictly match a "
               "workspace's ICP.",
        warning="Rides the heartbeat above, so it does nothing until that is on. Adds accounts, "
                "which are then refreshed on a schedule, so the cost compounds rather than being "
                "one-off. A workspace with an empty ICP discovers nothing, silently.",
        risk="high",
    ),
    SettingSpec(
        key="icp_discovery_daily_count", label="Discovery target per run", group=AUTOMATION,
        kind="int", minimum=1, maximum=100,
        effect="How many strictly matching net-new accounts discovery aims to add per workspace per "
               "run. A workspace's own setting wins over this default.",
        warning="Each target pulls a larger search pool and enrichment behind it, and every account "
                "added is then refreshed on a schedule, so the cost compounds.",
        risk="medium",
    ),
    SettingSpec(
        key="icp_discovery_min_fit", label="Discovery minimum ICP fit", group=AUTOMATION,
        kind="int", minimum=0, maximum=100,
        effect="Lowest ICP-fit score (0 to 100) a discovered company needs to be added.",
        warning="Lower adds weaker matches that reps must triage and that are then crawled on a "
                "schedule. Higher adds fewer.",
        risk="medium",
    ),
    SettingSpec(
        key="icp_discovery_interval_hours", label="Discovery interval (hours)", group=AUTOMATION,
        kind="int", minimum=1, maximum=168,
        effect="How often discovery runs for each opted-in workspace.",
        warning="Shorter runs a paid Exa search and candidate enrichment more often for every "
                "opted-in workspace.",
        risk="medium",
    ),
    SettingSpec(
        key="icp_discovery_enrich_max", label="Discovery candidates enriched", group=AUTOMATION,
        kind="int", minimum=0, maximum=200,
        effect="Most discovery candidates crawled for firmographics per run, before scoring.",
        warning="Each is a web crawl plus an LLM call. 0 scores candidates on search data alone, "
                "which cannot tell headcount or tech stack apart.",
        risk="medium",
    ),

    # ---- outreach and CRM -----------------------------------------------------------------------
    SettingSpec(
        key="cadence_enabled", label="Email cadences", group=OUTREACH, kind="bool",
        effect="Turns on the multi-touch cadence engine. The advance tick is a no-op until this "
               "is set.",
        warning="Cadence steps send real email to real prospects. Check the sending domain and the "
                "drafted copy before enabling.",
        risk="high",
    ),
    SettingSpec(
        key="cadence_batch_size", label="Enrollments advanced per tick", group=OUTREACH,
        kind="int", minimum=1, maximum=1000,
        effect="Most cadence enrollments one worker advances per tick.",
    ),
    SettingSpec(
        key="cadence_max_duration_days", label="Stop an enrollment after (days)", group=OUTREACH,
        kind="int", minimum=1, maximum=365,
        effect="An enrollment running longer than this is stopped mid-sequence.",
        warning="Lowering it stops long sequences that are already in flight on the next tick.",
        risk="medium",
    ),
    SettingSpec(
        key="campaign_sourced_min_send_confidence", label="Sourced address send bar",
        group=OUTREACH, kind="float", minimum=0, maximum=1,
        effect="The verification confidence an address the product found (rather than one a "
               "customer imported) must reach before a campaign sends to it.",
        warning="Lower sends bulk email to less-proven addresses, which raises bounces and can get "
                "the sending domain blocklisted.",
        risk="high",
    ),
    SettingSpec(
        key="crm_sync_enabled", label="Push to CRM", group=OUTREACH, kind="bool",
        effect="Pushes changed accounts out to each workspace's connected CRM.",
        warning="Writes into the customer's own CRM. Change-aware, so only stale or modified "
                "accounts move, but a mapping mistake reaches their production records.",
        risk="high",
    ),
    SettingSpec(
        key="crm_sync_batch_size", label="Accounts pushed per sweep", group=OUTREACH,
        kind="int", minimum=1, maximum=1000,
        effect="Most changed accounts claimed for a CRM push per heartbeat sweep.",
    ),

    # ---- money ----------------------------------------------------------------------------------
    SettingSpec(
        key="billing_enforcement", label="Billing enforcement", group=BILLING, kind="str",
        effect="off = no metering at all. shadow = evaluate and record every decision but never "
               "block. on = quotas and plans are enforced against customers.",
        warning="Moving from shadow to on is the moment plan limits become real. Anything shadow "
                "mode has been recording as 'would block' starts returning 402 to a paying "
                "customer. Read the would_block counter before flipping this.",
        risk="high", options=("off", "shadow", "on"),
    ),
    SettingSpec(
        key="billing_dunning_enabled", label="Dunning retries", group=BILLING, kind="bool",
        effect="Retries failed collections on a schedule and escalates to past-due.",
        warning="Turning it off stops chasing failed payments. The debt stays visible and is never "
                "voided, but nothing will try to collect it again.",
        risk="medium",
    ),
    SettingSpec(
        key="billing_dunning_schedule_days", label="Dunning schedule (days)", group=BILLING,
        kind="str", placeholder="1,3,7",
        effect="Days to wait before each retry of a failed payment, one number per retry: 1,3,7 "
               "retries after 1, 3 and 7 days, then stops.",
        warning="Retrying faster than this damages card authorization rates. The number of entries "
                "is the number of retries before an invoice stays past due.",
        risk="medium",
    ),

    # ---- access ---------------------------------------------------------------------------------
    SettingSpec(
        key="otp_registration_enabled", label="Two-step registration", group=ACCESS, kind="bool",
        effect="New sign-ups verify an emailed code before the account is created.",
        warning="Needs working outbound email. With email misconfigured, nobody can register at "
                "all, and the failure looks like a broken sign-up form rather than a mail problem.",
        risk="high",
    ),
    SettingSpec(
        key="admin_ip_allowlist", label="Control plane IP allowlist", group=ACCESS, kind="str",
        effect="Comma-separated IP addresses or CIDR ranges that may reach this Control plane. "
               "Empty means any address.",
        warning="At most two entries; use a CIDR range for an office. Get this wrong and you lock "
                "yourself out of the panel you would use to fix it, so read the address in the "
                "refusal message: behind a proxy it is often not the one you expect. A malformed "
                "list is ignored rather than enforced, which is the deliberate escape hatch.",
        risk="high", placeholder="203.0.113.10, 198.51.100.0/24",
    ),

    # ---- reliability ----------------------------------------------------------------------------
    SettingSpec(
        key="job_retry_enabled", label="Job retries", group=RELIABILITY, kind="bool",
        effect="Retries a failed background job with exponential backoff before parking it.",
        warning="Off degrades to fail-fast: the job is still dead-lettered with its payload "
                "intact, so evidence is kept and work is never silently lost. It just will not be "
                "attempted again automatically.",
        risk="medium",
    ),
    SettingSpec(
        key="idempotency_enabled", label="Idempotency keys", group=RELIABILITY, kind="bool",
        effect="De-duplicates requests carrying an Idempotency-Key header; a retry replays the "
               "first response instead of re-running the work.",
        warning="Needs Redis for cross-worker de-duplication. With the in-process backend and more "
                "than one API replica, two replicas will not see each other's keys.",
    ),
    SettingSpec(
        key="metrics_enabled", label="Prometheus metrics", group=RELIABILITY, kind="bool",
        effect="Serves /metrics for scraping.",
        warning="Turning this off makes the deployment blind to queue lag, 402 rates and dunning "
                "depth. It exists as a switch because an unpinned build once broke every endpoint "
                "through the instrumentator, not because running without it is normal.",
        risk="medium", requires_restart=True,
    ),
)

CATALOG: dict[str, SettingSpec] = {s.key: s for s in _SPECS}

# Named, not merely absent, so the reason survives. `test_the_forbidden_settings_are_never_settable`
# asserts none of these can reach the catalog.
FORBIDDEN: frozenset[str] = frozenset({
    "source_db_allow_private",
    "alert_webhook_allow_private",
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
    # Withheld 2026-09-11, and named so a request for them is told why:
    # * search_provider: in staging and prod, discovery is strictly Exa (`exa_search`) and reads
    #   nothing else. A picker would save, report "in effect" and change nothing. Platform health
    #   shows the routing that actually runs.
    # * payment_provider: switching to "noop" would silently stop collecting money, and money fails
    #   silently: it looks exactly like a quiet month. The Payment credentials screen governs it,
    #   with activation gated on a verification that proves WHICH Stripe account is live.
    "search_provider",
    "payment_provider",
    # Withheld 2026-09-15:
    # * email_verify_auth_header: a credential, and `current_values` returns values in plaintext.
    # * billing_support_credit_cap: widens what the `credits.grant.capped` permission can grant,
    #   which is a permissions decision, not a runtime setting.
    "email_verify_auth_header",
    "billing_support_credit_cap",
})


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Bring a JSON value to the type the setting expects, and refuse what does not fit.

    The panel posts JSON, so a boolean can arrive as the string "false", which is truthy in
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
