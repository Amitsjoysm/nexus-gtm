# NEXUS GTM — Data Substrate + Orchestrator Overhaul

> **Status:** Spec for Codex review. No implementation has begun. Sections marked
> **⚠️ CONFIRM** contain assumed REST/queue contracts for proprietary services that the
> owner must verify before the corresponding task is coded.

**Author:** Solution Architecture pass (Claude)
**Date:** 2026-06-04
**Branch of record:** `feat/conversational-orchestrator-frontend`
**Reviewers:** Codex (primary), Gemini (secondary)

---

## 1. Why this document exists

Two classes of problem block NEXUS from being a credible Pocus-class product:

1. **The data/agent substrate is synthetic.** Discovery backfills net-new companies by
   scraping DuckDuckGo result titles (employee_count is `null`); enrichment is a
   `first.last@domain` guess at 0.4 confidence; signals are a synthetic `DemoSignalSource`
   plus DDG news regex; the LLM defaults to a stub. None of this is sellable. We have a
   **fleet of real data sources** to wire in behind the existing adapter seams — proprietary
   data (**InfoJoy**), scraper-platform actors (**Apify**), neural/keyword **search engines**
   (**Exa.ai** and others), real-time stealth research (**CloakBrowser + Scrapegraph-ai**), and
   email verification (**everifier**) — composed through a **multi-source data registry** rather
   than any single vendor. InfoJoy is the highest-trust source, not the only source.

2. **The orchestrator does not understand the user.** The conversational intake is
   deterministic keyword/regex slot-filling. It cannot read a URL, cannot detect a location
   phrased freely, cannot accept a free-text ICP, and replies "I can identify companies" when
   the user says "I want prospects, not companies." It must understand context, then respond.

Plus two confirmed UI defects in the run console: the results table loses its shape when the
orchestrator chat is open, and the mini chat bar cannot be hidden.

This spec maps each external service onto an **existing adapter seam** (no architectural
rewrite), specifies the orchestrator intelligence layer, folds in the Tier 1–3 enhancements
from the SDR review, and fixes the two UI defects. It closes with a phased, Codex-reviewable
implementation plan.

### Non-negotiable constraints (carried from CLAUDE.md / project rules)

- **Production-grade**, "built like it's going into a real app used by millions."
- **Reduce external dependencies**; prefer stdlib + what's vendored. Every new dependency is
  justified inline and gated so the **offline test path (SQLite + stub LLM + in-memory queue)
  still passes with zero network**.
- Every endpoint stays **tenant-scoped + RBAC-gated**; never bypass `TenantSession`. New
  provider config uses the **`NEXUS_` prefix**; production must keep rejecting the insecure
  default JWT secret.
- New provider integrations follow the **adapter pattern already in the tree**
  (`EnrichmentProvider`, `BrowserProvider`, `SignalSource`, `LLMProvider`) so the stub
  implementations remain the default in tests.

---

## 2. Architecture overview — a multi-source data substrate

NEXUS does **not** bind to a single vendor. A **`DataSourceRegistry`** sits in front of the
existing adapter seams and composes many providers into a prioritized, deduplicated, provenance-
tagged result. InfoJoy is the highest-trust tier; Apify actors, search engines (Exa.ai et al.),
and CloakBrowser+Scrapegraph real-time research fill, extend, and verify the rest.

```
                          ┌──────────────────────────────────────────────────────┐
                          │                 NEXUS backend (FastAPI)               │
   PROPRIETARY            │                                                       │
   InfoJoy / IJUI  ──────▶│                                                       │
                          │   ┌───────────────── DataSourceRegistry ───────────┐  │
   SCRAPER PLATFORM       │   │  prioritized waterfall · dedupe by domain ·     │  │
   Apify actors    ──────▶│   │  per-field provenance + confidence · budgets    │  │
                          │   └──────┬───────────┬──────────────┬───────────────┘  │
   SEARCH ENGINES         │          │           │              │                  │
   Exa.ai / Brave / ─────▶│  CompanySearchProvider  EnrichmentProvider  SignalSrc  │
   Serper / Tavily        │          │           │              │                  │
                          │          ▼           ▼              ▼                  │
   REAL-TIME RESEARCH     │   DiscoveryAgent   waterfall     intent feed           │
   CloakBrowser (CDP) ───▶│   BrowserProvider  ──▶ SearchProvider (Exa/DDG/…)      │
   + Scrapegraph-ai  ────▶│   ResearchProvider ("super agent") + seed-ICP-from-URL │
                          │                                                       │
   VERIFICATION           │   EmailVerificationProvider ── separate host (everifier)│
   everifier (sep host)──▶│       async/bulk via Celery · suppression-aware        │
                          │                                                       │
                          │   Orchestrator intake ── LLM understanding layer       │
                          │       (slots / target / URL / location)                │
                          └──────────────────────────────────────────────────────┘
```

**Key principle:** every capability is an interface with at least three implementations —
**stub** (offline/test default), **one or more real clients**, and a **null/degraded** fallback.
Selection and ordering are by `NEXUS_*` settings, exactly like `settings.browser_provider` today.
No single source is load-bearing; if one is unconfigured or down, the registry routes around it.

### 2.1 The DataSourceRegistry (the new spine)

A small composition layer (`nexus/integrations/registry.py`) that, for each capability
(`company_search`, `enrich`, `signals`, `research`, `search`), holds an **ordered list of
providers** and applies a uniform policy:

- **Priority waterfall.** Try sources in tenant-configured order. Proprietary (InfoJoy) first,
  then Apify, then search-engine discovery, then real-time scrape — stop early once a result is
  "good enough" (capability-specific threshold), or fan out and merge when breadth matters
  (discovery).
- **Dedupe + merge.** Companies merge by normalized domain; contacts by email/identity. Fields
  merge by **highest-confidence-wins**, retaining a per-field `source` provenance so the UI can
  show "employee_count via InfoJoy, tech via Apify, news via Scrapegraph".
- **Provenance + confidence** travel with every field (typed `Sourced[T] = {value, source,
  confidence, fetched_at}`), so the relevance engine and the grounded-send gate can reason about
  data quality.
- **Per-source budgets + circuit breakers.** Each provider has a cost/quota budget and a
  failure-trip that takes it out of rotation temporarily; the registry degrades gracefully and
  emits a run event rather than failing.
- **Caching.** Keyed by `(capability, normalized query)`, TTL per source class (proprietary long,
  real-time short), reusing existing DB/blackboard patterns.

The registry is the **only** thing `DiscoveryAgent`, the enrichment waterfall, and the research
tool talk to. Adding a new vendor later = register a provider, no call-site changes.

### Source → capability map

| Source | Capabilities it serves | Tier / role |
|---|---|---|
| **InfoJoy / IJUI** | company_search, enrich (reveal), signals (intent), buying-committee | **Proprietary, highest trust.** Real firmographics, verified contacts. Self-hosted sidecar. |
| **Apify actors** | company_search, enrich, signals | **Scraper platform.** Configurable actors (e.g. company/contact/jobs/news scrapers) run via REST; breadth + net-new where proprietary is thin. |
| **Exa.ai** (+ Brave/Serper/Tavily) | search, company_search (discovery), research seeds | **Search engines.** Neural/keyword search to find net-new companies and feed better URLs to the research super-agent. |
| **CloakBrowser** | search, fetch (for research/enrich) | **Real-time stealth browser** over CDP for pages that block plain HTTP. |
| **Scrapegraph-ai** | research (super-agent), seed-ICP-from-URL | **Real-time structured extraction.** LLM-driven page/search → typed grounded facts. |
| **everifier** | verify (email) | **Verification, separate host.** Gates outbound + feeds suppression. |
| **DuckDuckGo (existing)** | search | **Free fallback** when no paid search engine is configured. |
| **Stub providers** | all | **Offline/test default.** Deterministic fixtures, zero network. |

---

## 3. Component specs

> Every provider in §3.1–§3.6 is **registered with the `DataSourceRegistry` (§2.1)**, not called
> directly by agents. Order/priority is config; the registry handles waterfall, dedupe, merge,
> provenance, budgets, and circuit-breaking. InfoJoy is described first because it is the
> highest-trust tier — but the call sites are source-agnostic.

### 3.1 InfoJoy / IJUI — proprietary data provider (highest-trust tier)

**What it is (researched):** ASP.NET Core API (default `:5000`) with controllers incl.
`CompanySearch`, `Search`, `AISearch`, `Reveal`, `BuyingCommittee`, `Suppression`,
`Sequencing`, `Export`, `MyList`, `SavedSearch`, `Activity`, `Billing`, `Auth`. Entities:
`Company`, `Contact`, `ContactReveal`, `SuppressionList`, `Tenant`, `UserList`, `UserSequence`.

**Confirmed company-search contract (from `CompanySearchController.cs`):**

- `POST /SearchView_Account` → filter option lists.
- `POST /GetAccountsSearch` and `POST /GetAccountsSearchSorted` with `AccountSearchRequest`:
  `CompanyName, CompanyUrl, IndustryType, SubIndustry, EmployeeSize (["1-10","11-50",…]),
  Revenue, Country, State, City, Technology, SicCode, ZipCode, Keyword, PageNumber,
  RowsPerPage, SortColumn, SortDir` → returns `{ headers: [company…], SearchCount,
  PaginationStr }`.
- Company fields: `CompanyID, CompanyName, CompanyURL, City, State, Country, Phone,
  EmployeeSize, Revenue, IndustryType, SubIndustry, Technology, SIC_code, About, ZipCode,
  Address`.
- `POST /BulkAddCompaniesToList`.

**⚠️ CONFIRM with owner** — the people/reveal contracts were not fully enumerable from public
source. The following are **assumed** and must be verified before coding the reveal path:

- `POST /Reveal` (or `/GetContactReveal`) request/response: which identifier unlocks a contact
  (ContactID? CompanyID + persona?), what the response returns (email, phone, confidence,
  `verified` flag), and **credit accounting** semantics (does a reveal consume a tenant credit;
  is it idempotent on re-reveal).
- `POST /BuyingCommittee` request (CompanyID + target personas/titles) → contact list shape.
- Suppression endpoints (`/Suppression…`) read/write contract.
- AuthN: API key header vs. bearer token; per-tenant key vs. shared key + tenant param.
- Whether intent/AISearch exposes a signal feed we can poll, and at what granularity.

**Integration design:**

1. **New client module** `nexus/integrations/infojoy/client.py` — a thin async `httpx` client
   with: `search_companies(AccountSearchRequest) -> AccountSearchResponse`,
   `reveal_contact(...)`, `buying_committee(...)`, `suppression_*`. All methods typed with
   Pydantic models in `nexus/integrations/infojoy/models.py`. Base URL + key from
   `NEXUS_INFOJOY_BASE_URL` / `NEXUS_INFOJOY_API_KEY` (per-tenant override via tenant config).
2. **`CompanySearchProvider` interface** (`nexus/integrations/company_search.py`) with
   `InfoJoyCompanySearchProvider`, plus `StubCompanySearchProvider` (deterministic fixtures for
   tests). `DiscoveryAgent` no longer calls `ctx.browser.search` directly — it calls
   `registry.company_search(icp)`, and InfoJoy is the first registered provider. The provider
   maps NEXUS ICP → `AccountSearchRequest` (industries→IndustryType, employee_min/max→EmployeeSize
   bands, countries→Country, required_tech→Technology) and maps returned companies → `Account`
   rows with **real `employee_count`, revenue, tech, SIC** (no more null firmographics). Each row
   carries `source="infojoy"` and per-field provenance so the registry can merge it with Apify /
   search-engine results.
3. **Enrichment via Reveal** — `InfoJoyEnrichmentProvider` implements the existing
   `EnrichmentProvider` ABC and sits **first in the waterfall**, ahead of `PatternEmailProvider`
   (kept as last-resort guess). Reveal returns verified email/phone with confidence; pattern
   guess only fills gaps.
4. **Intent as a SignalSource** — `InfoJoyIntentSource(SignalSource)` polls InfoJoy intent (⚠️
   pending contract) and emits `RawSignal`s into the existing `SIGNAL_LIBRARY` kinds (map
   InfoJoy intent topics → `g2_intent`, `funding`, etc., preserving strength weights).
5. **Hosting** — InfoJoy is "hosted through this app". Decision: run it as a **sidecar service**
   (its own container: API + SQL Server + Redis + Elasticsearch), reached over the private
   network by base URL. NEXUS does **not** embed ASP.NET; it only consumes the REST API. This
   keeps the dependency surface a network boundary, not a code dependency.

**Credits & cost guardrails:** reveals cost money. Add `NEXUS_INFOJOY_REVEAL_BUDGET` (per-tenant
daily cap) enforced in `InfoJoyEnrichmentProvider`; over-budget reveals queue rather than fail,
and the run surfaces a "reveal budget reached" event.

### 3.2 everifier — separate-host email verification

**What it is (researched):** Python (FastAPI-style) backend + **Celery** for bulk. Files:
`server.py`, `routes_verification.py` (single + bulk), `routes_apikeys.py` (API-key auth),
`email_verifier.py`, `email_service.py`, `celery_app.py`, `tasks.py`, `models.py`,
`database.py`, `config.py`. Deployed **on its own host/IP** so bulk SMTP probing never touches
the NEXUS sending domain or IP (deliverability hygiene).

**⚠️ CONFIRM contract** (assumed from file names; verify):

- `POST /verify` (single): `{ email }` → `{ email, status: "valid|invalid|risky|unknown",
  reason, mx, smtp, disposable, role_account, score }`.
- `POST /verify/bulk`: `{ emails: [...] , webhook_url? }` → `{ job_id }`; poll
  `GET /verify/bulk/{job_id}` → `{ status, results: [...] }`, or push to `webhook_url`.
- AuthN: `X-API-Key` header (per `routes_apikeys.py`).

**Integration design:**

1. **`EmailVerificationProvider` ABC** (`nexus/verification/provider.py`) with:
   - `EverifierProvider` — async `httpx` client to `NEXUS_EVERIFIER_BASE_URL` with
     `NEXUS_EVERIFIER_API_KEY`. `verify_one(email)` and `verify_bulk(emails) -> job handle`.
   - `StubEmailVerifier` — offline default: deterministic status by simple rules (syntax +
     known-disposable list), zero network. Tests use this.
2. **Where it plugs in:**
   - **Single-verify** in the enrichment waterfall: after a reveal/guess produces an email,
     verify before it is allowed into a draft. Store `email_status` + `email_score` on the
     contact.
   - **Bulk-verify** as an **orchestration step** (`verify_contacts`) for list-level
     verification, dispatched to everifier's Celery via the bulk endpoint; the run parks on a
     poll/webhook callback (reuse the existing async run-step + RunEvent pattern, like the
     approval gate parks outbound steps).
3. **Suppression coupling:** `invalid` / `disposable` / `role_account` results feed a NEXUS
   suppression list (and, if confirmed, sync to InfoJoy `/Suppression`). The
   **grounded-messaging gate** (see §5) refuses to send to anything not `valid`.
4. **Separate host is mandatory** — codify it: NEXUS never runs SMTP probes itself; the only
   email-verification path is the everifier provider. A config validator rejects pointing
   `NEXUS_EVERIFIER_BASE_URL` at the NEXUS host in production.

### 3.3 CloakBrowser — real stealth CDP browser

**What it is (researched):** `pip install cloakbrowser`; returns a Playwright `Browser`. Docker
CDP server `cloakserve` on `:9222`; connect with `playwright.chromium.connect_over_cdp(
"http://localhost:9222")`. Per-connection fingerprint via query params; options for
proxy/geoip/humanize/headless.

**Integration design:**

1. Replace the `CloakCDPBrowser` stub in `nexus/enrichment/browser.py` (currently a DDG
   fallback) with a real implementation behind the existing `BrowserProvider` ABC:
   - `connect_over_cdp(NEXUS_CLOAK_CDP_URL)` lazily; reuse the connection; one context per
     scrape with fingerprint/proxy/geoip query params from settings.
   - Implements `search(query)` and a new `fetch(url) -> rendered_html/text` used by the
     research provider for JS-heavy pages.
   - **Graceful degradation:** if CDP is unreachable, fall back to `DuckDuckGoSearch`
     (httpx) and log a degraded-mode event — never hard-fail a run on browser unavailability.
2. **Playwright is an optional dependency**, imported lazily inside the Cloak provider only.
   `settings.browser_provider` default stays `duckduckgo`/stub in tests. Document
   `playwright install` + `cloakserve` as a deploy step, not a test dependency.
3. Concurrency + politeness: a bounded semaphore (`NEXUS_CLOAK_MAX_CONCURRENCY`) and per-domain
   rate limit so stealth scraping stays well-behaved.

### 3.4 Scrapegraph-ai — research super-agent

**What it is (researched):** `pip install scrapegraphai` + `playwright install`.
`SmartScraperGraph` (prompt + single URL), `SearchGraph` (prompt across search results),
`SmartScraperMultiGraph`. Config needs an `llm` section (e.g. `openai/gpt-4o-mini` or
`ollama/llama3.2`).

**Integration design:**

1. **`ResearchProvider` ABC** (`nexus/research/provider.py`):
   - `StubResearchProvider` — offline default; returns templated facts (mirrors current stub
     research-brief behavior). Tests use this.
   - `ScrapegraphResearchProvider` — wraps `SmartScraperGraph` / `SearchGraph`. Reuses the
     **NEXUS-configured LLM** (point Scrapegraph's `llm` config at the same
     `OpenAICompatProvider` settings so we don't introduce a second LLM config surface) and
     **CloakBrowser** as the fetch layer where Scrapegraph allows a custom loader; otherwise its
     own Playwright with the same proxy settings.
2. **Behind `ResearchTool`:** `ResearchTool` calls `ResearchProvider.research(account, prompt)`
   returning **structured, grounded facts** (typed schema: pains, initiatives, tech, hiring,
   news, citations) written to the run blackboard. `ComposeMessageTool` already sets
   `draft.grounded = bool(research facts)` — real facts now make grounding meaningful.
3. **Two new capabilities the super-agent unlocks:**
   - **Seed ICP from URL** (used by intake, §4): given a company/product URL, extract what they
     sell + who they sell to → propose ICP slots. New `ResearchProvider.profile_from_url(url)`.
   - **Deep account research** for the research run recipe: `SearchGraph` across the company's
     site + news for a grounded brief with citations.
4. **Cost/lat guardrails:** per-run research budget + cache (`account_id + prompt hash` →
   facts, TTL) so repeated runs don't re-scrape. Caching reuses the existing blackboard/DB
   patterns.

### 3.5 Apify — scraper-platform actors

**What it is:** Apify is a hosted platform of reusable scrapers ("actors") with a uniform REST
API. We run an actor with an input payload, it produces a dataset, and we read the items back.
This gives breadth and net-new coverage (company directories, contact scrapers, job-post and
news scrapers) without us writing/maintaining each scraper.

**Contract (stable, public Apify API):**

- Run an actor: `POST https://api.apify.com/v2/acts/{actorId}/runs?token=…` with the actor's
  `input` JSON → `{ data: { id (runId), defaultDatasetId, status } }`.
- Synchronous convenience: `POST /v2/acts/{actorId}/run-sync-get-dataset-items?token=…` returns
  dataset items directly (best for short runs).
- Poll: `GET /v2/actor-runs/{runId}` → status (`READY|RUNNING|SUCCEEDED|FAILED`).
- Fetch results: `GET /v2/datasets/{datasetId}/items?token=…` → `[ {…}, … ]`.
- Auth: `token` query param or `Authorization: Bearer` (`NEXUS_APIFY_TOKEN`).

**⚠️ CONFIRM with owner:** *which actors* we standardize on per capability (e.g. a company-data
actor, a contact/email actor, a jobs/news actor) and their **exact input/output schemas** —
actor schemas vary. Each chosen actor gets a small **mapper** to NEXUS models. Make actor IDs
configurable (`NEXUS_APIFY_ACTORS` = a capability→actorId map) so we never hard-code a vendor's
actor.

**Integration design:**

1. **Client** `nexus/integrations/apify/client.py` — async `httpx`: `run_actor(actor_id, input)
   -> items`, with run-sync for short actors and run+poll+dataset for long ones. Bounded poll
   with timeout; surfaces a degraded event on actor failure (registry circuit-breaks it).
2. **Providers** behind the registry, one per capability we use Apify for:
   - `ApifyCompanySearchProvider(CompanySearchProvider)` — ICP → actor input → `Account` rows
     (`source="apify"`, per-field provenance). Sits **after** InfoJoy in the waterfall (fills
     net-new / fields InfoJoy lacks).
   - `ApifyEnrichmentProvider(EnrichmentProvider)` — contact/email enrichment actor; slots into
     the enrichment waterfall between InfoJoy reveal and the pattern guess.
   - `ApifySignalSource(SignalSource)` (optional) — jobs/news actors → `RawSignal`s mapped to
     `SIGNAL_LIBRARY` kinds (hiring, news, funding).
3. **Per-actor budget** (`NEXUS_APIFY_BUDGET`) and result cache; the registry enforces both.
4. **Stub** `StubApifyClient` returns fixture dataset items offline — Apify providers are fully
   testable with zero network.

### 3.6 Search engines — Exa.ai (and pluggable others)

**What it is:** the current `BrowserProvider.search` is DuckDuckGo HTML scraping. We promote
search to its own **`SearchProvider` capability** with high-quality engines, primarily
**Exa.ai** (neural + keyword search with structured results and optional content extraction),
and leave the door open for Brave / Serper / Tavily — all simple REST + API key.

**Contract (Exa.ai, stable public API):**

- `POST https://api.exa.ai/search` with `{ query, type: "neural"|"keyword"|"auto", numResults,
  category?, includeDomains?, contents?: { text, highlights } }`, header `x-api-key:
  NEXUS_EXA_API_KEY` → `{ results: [ { title, url, publishedDate, author, text?, highlights? } ] }`.
- `POST /findSimilar` (similar pages by URL) and `/contents` (extract page text) available for
  research seeding.

**Integration design:**

1. **`SearchProvider` ABC** (`nexus/integrations/search/provider.py`): `search(query, *, kind,
   num) -> list[SearchResult]` and optional `find_similar(url)`. Implementations:
   `ExaSearchProvider`, `DuckDuckGoSearchProvider` (wraps existing DDG, the free fallback), and
   `StubSearchProvider` (offline fixtures). Room for `BraveSearchProvider` / `SerperSearchProvider`
   later — register and go.
2. **Two consumers:**
   - **Discovery** — the registry can use search-engine results as a `CompanySearchProvider`-ish
     source (extract company domains from results), positioned after Apify, ahead of plain
     real-time scraping. Exa's `category: "company"` + `includeDomains` make this targeted.
   - **Research seeding** — `ScrapegraphResearchProvider` (§3.4) asks the `SearchProvider` for the
     best URLs, then `SearchGraph`/`SmartScraperGraph` (over **CloakBrowser**) extracts grounded
     facts. Exa's neural search dramatically improves which pages we scrape.
3. **`find_similar`** powers a "find lookalike companies" play (Tier 3) — seed with a won
   account's URL, get similar companies, score them through the relevance engine.
4. **Selection + budget:** `NEXUS_SEARCH_PROVIDER` (ordered list, default `duckduckgo` so tests
   and unconfigured deploys still work) and `NEXUS_EXA_BUDGET`. DDG remains the zero-cost floor.

**CloakBrowser + Scrapegraph as the real-time tier (recap):** §3.3 + §3.4 already cover the
real-time research path. With search engines added, the full real-time research flow is:
**`SearchProvider` (Exa) finds URLs → `BrowserProvider` (CloakBrowser CDP) fetches them past
bot-walls → `ResearchProvider` (Scrapegraph) extracts typed grounded facts → blackboard.** This
is the orchestrator's "super agent."

---

## 4. Orchestrator intelligence overhaul (the core bug fix)

**Locus:** `nexus/orchestration/intake.py` (+ `chat_service.py` for wiring). Today
`extract_slots` is keyword/regex only (no URL regex; fixed `_COUNTRY_ALIASES` gazetteer);
`infer_target` keys on contact/people/persona/title/decision-maker but **misses
prospect/leads/buyers**, and is **sticky** (`if current in (TARGET_COMPANIES, TARGET_CONTACTS):
return current`), so it can't re-target; decisioning is deterministic-only, so a free-text ICP
gets a preset reply.

### 4.1 Design — an LLM understanding layer with deterministic fallback

Add a single **understanding pass** per user turn that runs **before** the deterministic
slot-filler and can override it. Deterministic extraction stays as the **offline/test fallback**
and as a cheap fast-path, preserving the zero-network test guarantee.

**New `understand(text, icp_state, target, pending_slot) -> Understanding` (LLM, purpose
`"intake_understanding"`):** one structured call returning:

```
Understanding {
  target: "companies" | "contacts" | null      # detects "prospects/leads/buyers" → contacts
  slots: { industries?, geo?, employee_min/max?, required_tech?, titles?,
           intent_signals?, exclusions? }       # free-text ICP accepted, normalized to slots
  url: string | null                            # any URL the user pasted
  location: string | null                       # free-text location → geo
  freeform_icp: string | null                   # verbatim ICP if user dictated one
  intent: "provide_icp" | "answer_question" | "change_target" | "smalltalk" | "launch"
  reply_hint: string                            # what to say next, in context
}
```

Wiring in `IntakeController.advance`:

1. Run `understand(...)`. If the LLM is the stub/offline, `understand` returns `null` fields and
   we **fall through to the existing deterministic extractor** (no behavior regression in tests).
2. Merge `Understanding.slots` over the deterministic extraction (LLM wins on conflict for
   free-text; deterministic wins for high-confidence regex like explicit size bands).
3. **Target re-detection is no longer sticky:** if `Understanding.target == "contacts"` (or
   deterministic detects prospect/leads/buyers — add these synonyms to `infer_target`), switch
   target even if currently `companies`. "I don't want companies, I want prospects" now flips to
   contacts and the next reply acknowledges it.
4. **URL detected →** call `ResearchProvider.profile_from_url(url)` (Scrapegraph) to seed ICP
   slots, then ask the user to confirm/adjust ("Based on acme.com you sell X to Y — target
   companies like these?"). This is the "dynamic, builds correct ICP" behavior requested.
5. **Location detected →** map free-text to `geo` slot (drop the fixed gazetteer dependency for
   the LLM path; keep gazetteer for fallback).
6. **Free-text ICP accepted →** if `freeform_icp` present, store it and **echo back a structured
   interpretation for confirmation** rather than re-asking preset questions.
7. The next assistant message is phrased from `reply_hint` (purpose `clarify_question`), so
   replies are **contextual**, not preset.

### 4.2 New synonyms / extraction (deterministic fallback, also improves offline)

- `infer_target`: add `prospect, prospects, lead, leads, buyer, buyers, decision-maker,
  champion` → `contacts`; add `account, accounts, organization, vendor` → `companies`. Remove
  the unconditional sticky return; allow re-targeting when the new turn clearly names the other.
- Add a **URL regex** to `extract_slots` so even the offline path captures a pasted URL into a
  new `seed_url` slot.
- Add a generic **location capture** (city/state/country word after "in/near/based in …") so
  free-text locations land in `geo` without requiring the gazetteer.

### 4.3 Contract & tests

- `understand` returns a typed Pydantic `Understanding`; `StubLLMProvider` returns an empty
  understanding (all `null`, `intent` inferred from deterministic layer) so **existing intake
  tests pass unchanged**.
- New tests (offline, stub-LLM): prospect→contacts re-target; pasted URL captured to `seed_url`;
  free-text location → geo; free-text ICP stored + confirmation phrased. These assert the
  deterministic improvements (synonyms, URL/location regex) independent of the LLM.
- New tests (LLM path) use a scripted fake LLM returning canned `Understanding` JSON to assert
  merge precedence, re-targeting, and URL-seeding wiring — still no network.

---

## 5. Tier 1–3 enhancements (folded in)

Each maps to a seam above; listed with the seam it rides so Codex sees they're not new
architecture.

**Tier 1 — credibility (must-have to sell):**
- **Multi-source enrichment waterfall + verification** — registry order InfoJoy reveal → Apify
  enrich → pattern guess → everifier verify (§2.1, §3.1, §3.2, §3.5). Real firmographics from the
  company-search waterfall (InfoJoy → Apify → search engines) kill null `employee_count`, with
  per-field provenance.
- **Grounded-messaging gate** — `SendMessageTool` (already `requires_approval`) additionally
  refuses to send unless `draft.grounded` is true **and** the recipient email is everifier-
  `valid`. Approval UI shows the grounding facts, **per-field source provenance**, and
  verification status.
- **Real intent signals** — registry `SIGNAL_SOURCES` (InfoJoy intent + Apify jobs/news)
  replaces synthetic `DemoSignalSource` in prod (stub stays in tests).

**Tier 2 — workflow stickiness:**
- **Bi-directional CRM sync + routing** — new `CRMConnector` ABC (mirror the SEP connector
  pattern already present); push enriched accounts/contacts + activity, pull ownership for
  routing. (Salesforce/HubSpot adapters; stub default.) ⚠️ out of scope to fully build now —
  spec the interface, implement stub + one real adapter in a later phase.
- **Alert delivery (Slack/email)** — `AlertChannel` ABC; Slack webhook + email via everifier-
  validated sender. Rides the existing EventBus → alert pipeline.
- **Triage-grade inbox** — surface verification + grounding + intent recency in the inbox row so
  reps triage in one glance (frontend; consumes fields added above).

**Tier 3 — differentiation:**
- **Outcome-feedback loop** — capture send→reply→meeting outcomes, feed back into relevance
  weights (per-tenant learned weights overriding the static 0.35/0.30/0.15/0.20). New
  `outcomes` table + a nightly reweight job. ⚠️ spec interface now, implement later phase.
- **Research super-agent deep briefs** (§3.3+§3.4+§3.6) — Exa finds URLs → CloakBrowser fetches →
  Scrapegraph extracts grounded briefs with citations.
- **Find-lookalike-companies play** — Exa `find_similar` seeded with a won account's URL →
  registry company merge → relevance scoring (§3.6).
- **Onboarding flow + manager attribution dashboards** — frontend, later phase.

---

## 6. Frontend fixes (confirmed defects)

**Locus:** `frontend/src/pages/RunDetailPage.tsx` + `ResultsPanel.module.css` +
`RunDetailPage.module.css`.

### 6.1 Results table loses shape when chat is open

Root cause: the results `DataTable` inside `.main` has no horizontal-overflow containment, so
when the `aside.dock` opens and narrows `.main`, columns are squeezed/overflow.

Fix:
- Wrap the results table in a scroll container: `overflow-x: auto; min-width: 0;` on the table's
  flex/grid parent so the table keeps its natural column widths and scrolls horizontally instead
  of deforming. Ensure the `.main` grid child has `min-width: 0` (the classic flex/grid
  overflow fix).
- Define explicit `min-width` per column (token-based) so the table holds shape; the panel
  scrolls rather than collapsing cells.
- Verify at all breakpoints with the dock both open and closed (impeccable + ui-ux-pro-max:
  contrast, focus, 44px targets preserved).

### 6.2 No way to hide the mini chat bar

Root cause: `dockToggle` exists but only renders when `chatSessionId` and doesn't truly hide the
dock on desktop; the mini chat is always present.

Fix:
- Make the dock a **proper collapsible panel at all breakpoints**, controlled by
  `chatOpen` state, with a persistent, always-visible toggle (open *and* close affordance).
  Default the dock **closed** so the results table gets full width unless the user opens chat.
- The toggle is a real `<button aria-expanded={chatOpen} aria-controls="run-chat-dock">` with a
  visible focus ring; closing returns focus to the toggle. When closed, the dock is removed from
  the layout (not just visually hidden) so the table reflows to full width.
- Reduced-motion: dock open/close uses a transform/opacity transition with a
  `prefers-reduced-motion` instant fallback (framer-motion conventions).

Both fixes use design tokens only (no magic numbers) and keep loading/empty/error states.

---

## 7. Configuration surface (new `NEXUS_` settings)

All optional, all defaulting to the **offline stub** so tests need no env:

```
# Registry — ordered provider lists per capability (CSV, left = highest priority).
# Defaults keep tests/unconfigured deploys on stubs + the free DDG floor.
NEXUS_COMPANY_SEARCH_SOURCES   (default: stub)      e.g. infojoy,apify,exa
NEXUS_ENRICH_SOURCES           (default: stub)      e.g. infojoy,apify,pattern
NEXUS_SIGNAL_SOURCES           (default: demo)      e.g. infojoy,apify,webnews
NEXUS_SEARCH_PROVIDER          (default: duckduckgo) e.g. exa,brave,duckduckgo
NEXUS_RESEARCH_PROVIDER        (default: stub)      e.g. scrapegraph

# InfoJoy (proprietary, highest trust)
NEXUS_INFOJOY_BASE_URL / NEXUS_INFOJOY_API_KEY / NEXUS_INFOJOY_REVEAL_BUDGET

# Apify (scraper platform)
NEXUS_APIFY_TOKEN / NEXUS_APIFY_ACTORS (capability→actorId map) / NEXUS_APIFY_BUDGET

# Search engines
NEXUS_EXA_API_KEY / NEXUS_EXA_BUDGET            (+ NEXUS_BRAVE_* / NEXUS_SERPER_* later)

# Real-time research browser
NEXUS_CLOAK_CDP_URL / NEXUS_CLOAK_PROXY / NEXUS_CLOAK_MAX_CONCURRENCY / NEXUS_RESEARCH_BUDGET

# Verification (separate host, mandatory)
NEXUS_EVERIFIER_BASE_URL / NEXUS_EVERIFIER_API_KEY   # prod: must NOT equal NEXUS host
```

Provider selection mirrors today's `settings.browser_provider`, but capabilities now take an
**ordered list** consumed by the `DataSourceRegistry` (§2.1). Config validators: (a) prod rejects
everifier base URL == NEXUS host; (b) prod requires real keys/tokens for any non-stub provider
named in a `*_SOURCES` list; (c) unknown provider names fail fast at startup; (d) the insecure-JWT
rejection stays. Per-tenant overrides of the ordered lists are allowed via tenant config.

---

## 8. Phased implementation plan (Codex-reviewable)

Each phase is independently shippable, keeps the suite green, and defaults to stubs. Build via
`superpowers:subagent-driven-development` after Codex sign-off on this spec.

**Phase 0 — Registry + provider scaffolding (no behavior change).**
Add the capability ABCs (`CompanySearchProvider`, `EnrichmentProvider` already exists,
`SignalSource` already exists, `SearchProvider`, `ResearchProvider`, `EmailVerificationProvider`),
the **`DataSourceRegistry`** (priority waterfall, dedupe/merge by domain/email, `Sourced[T]`
provenance, per-source budget + circuit breaker, cache), stub implementations for every provider,
`NEXUS_*_SOURCES` ordered-list settings + selection plumbing + config validators. Re-point
`DiscoveryAgent` and the enrichment path at the registry (still resolving to stubs). All default
to stub/DDG. Tests assert stub selection + merge/dedupe/provenance logic offline. *No real
external client yet — this is the spine the rest plug into.*

**Phase 1 — Orchestrator intelligence.**
`understand()` pass, `Understanding` model, deterministic synonym/URL/location improvements,
re-targeting (un-stick `infer_target`), free-text ICP acceptance, URL→seed-ICP wiring (against
`StubResearchProvider.profile_from_url`). New offline tests. This fixes the reported orchestrator
bugs with zero new external dependencies.

**Phase 2 — Frontend run-console fixes.**
Results table shape-hold + collapsible/hideable chat dock. Pure frontend; impeccable +
ui-ux-pro-max pass; verified at all breakpoints.

**Phase 3 — Search engines (Exa.ai) + CloakBrowser + Scrapegraph research.**
`SearchProvider` (`ExaSearchProvider` + DDG wrapper + stub) registered as the search capability;
real `CloakCDPBrowser` (CDP connect, lazy Playwright, DDG degrade); `ScrapegraphResearchProvider`
behind `ResearchTool` wired as **Exa→CloakBrowser→Scrapegraph** real-time research; deep-brief
recipe + research cache/budget. Real facts make `draft.grounded` meaningful. Search engines also
register as a discovery source in the company-search waterfall.

**Phase 4 — Apify actors.** *(⚠️ gated on confirmed actor IDs + schemas)*
`apify/client.py` (run-sync + run/poll/dataset, stub client), `ApifyCompanySearchProvider`,
`ApifyEnrichmentProvider`, optional `ApifySignalSource`, per-actor mappers + budget. Registered
in the waterfall after InfoJoy/before plain scrape. Configurable actor IDs.

**Phase 5 — InfoJoy data substrate.** *(⚠️ gated on confirmed reveal/intent contracts)*
`infojoy/client.py` + models, `InfoJoyCompanySearchProvider` (real firmographics, top of the
waterfall), `InfoJoyEnrichmentProvider` (reveal-first), `InfoJoyIntentSource`. Sidecar deploy doc.

**Phase 6 — everifier verification.** *(⚠️ gated on confirmed verify contracts)*
`EverifierProvider` (single + bulk/Celery), `verify_contacts` orchestration step (park/poll/
webhook), suppression coupling, grounded+verified send gate. Separate-host config validator.

**Phase 7 — Enhancements.**
Alert channels (Slack/email), triage-grade inbox fields (incl. provenance), CRM connector
interface + one adapter, outcome-feedback table + reweight job, find-lookalike play,
onboarding/attribution. Each as its own task.

**Cross-cutting per phase:** offline tests stay green; new deps lazy-imported + justified;
tenant-scoping/RBAC preserved; `code-review-graph update` after structural changes; Codex review
per phase.

---

## 9. Open questions for the owner (must answer before the gated phases)

1. **InfoJoy reveal contract** — endpoint, identifier, response shape, credit/idempotency
   semantics (§3.1 ⚠️). *(Phase 5)*
2. **InfoJoy buying-committee + suppression** request/response shapes. *(Phase 5)*
3. **InfoJoy intent feed** — is there a pollable intent endpoint, topics, granularity? *(Phase 5)*
4. **InfoJoy auth** — per-tenant API key vs shared key + tenant param. *(Phase 5)*
5. **Apify actors** — which actor IDs per capability (company / contact / jobs / news), and their
   input/output schemas (§3.5 ⚠️). *(Phase 4)*
6. **Exa.ai (and any other search engines)** — confirmed plan/quota; whether we also want
   Brave/Serper/Tavily registered now or later (§3.6). *(Phase 3)*
7. **everifier** — exact `/verify` + `/verify/bulk` request/response, auth header, webhook vs
   poll for bulk (§3.2 ⚠️). *(Phase 6)*
8. **Source priority defaults** — confirm the intended waterfall order per capability
   (proposed: InfoJoy → Apify → search-engine/real-time), and any per-tenant overrides.
9. **Hosting topology** — confirm InfoJoy + everifier are reachable as private-network sidecars
   with stable base URLs; confirm everifier is on a **distinct sending domain/IP**.
10. **LLM for production understanding + Scrapegraph** — which `OpenAICompat` model/endpoint;
    acceptable per-turn/per-run cost budgets.

---

## 10. What is intentionally NOT in this spec

- No change to the run engine's durability/approval model — we reuse park/poll/event patterns.
- No second LLM config surface — Scrapegraph points at the existing `OpenAICompat` settings.
- No embedding of InfoJoy's ASP.NET code — it stays a network boundary.
- No removal of the offline stub path — it remains the default and the test substrate.
```