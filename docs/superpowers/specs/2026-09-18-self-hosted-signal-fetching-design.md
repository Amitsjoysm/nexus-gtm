# Self-hosted signal fetching — design

**Date:** 2026-09-18
**Status:** approved, not yet implemented
**Scope:** signal collection only. Enrichment/research and discovery/lookalikes are separate
sub-projects with their own specs (see *Out of scope*).

## The problem

Signal collection re-buys the same searches on a six-hour loop.

`DorkedSearchSource` issues up to `signal_dork_max_queries` (4) billed searches per account per
crawl, through `NEXUS_SIGNAL_SEARCH_PROVIDER=firecrawl` at a measured $0.0064 per search. A HOT
account refreshes every `account_refresh_interval_s` (6h), so it costs about $0.10/day — roughly
**$3/month per hot account, ~$3,070/month at 1,000 hot accounts** — to ask the same four questions
about the same company four times a day.

Almost none of it is cached. `DataSourceRegistry`'s cache is an in-process dict: it dies with the
process, is not shared between the API and the worker, and `DorkedSearchSource` bypasses the
registry entirely by building its provider directly. Two tenants tracking one company buy the same
answer twice; the same tenant buys it again six hours later.

Meanwhile the free path is already fragile. `WebNewsSource` runs on `get_browser_provider()`, which
resolves to a plain-httpx DuckDuckGo scraper that 403s after roughly ten rapid queries.

**Scrapling is already in the repo and already inert.** `pyproject.toml` declares
`scraping = ["scrapling>=0.4"]` and `nexus/enrichment/browser.py` has a `ScraplingBrowser`, but
`deploy/Dockerfile` installs only `.[postgres,redis,migrate,metrics]`, so it is never present and
`browser_provider="auto"` silently falls through to httpx. The adapter also parses a DuckDuckGo SERP
with regex rather than using Scrapling's parser, and fetches no pages at all. This is the
"configured and doing nothing" state this codebase keeps having to diagnose.

## What this does NOT change

Signal kinds, `_classify_news`, `event_dedupe_key`, the alert floor and routing, the hot/cold
tiering and `next_refresh_at`, plan gates, and **every price a customer pays**. Three things change:
where a search result comes from, whether we buy it a second time, and how often each kind is
searched.

## Decisions (locked with the product owner, 2026-09-18)

| Decision | Choice | Why |
|---|---|---|
| Order of work | Signals first; enrichment/research second; discovery + Apify last | Signals are the repetitive spend; discovery needs an index we do not have |
| What may be scraped | First-party sources plus tolerant SERP endpoints (DuckDuckGo, Brave, Startpage), paid fallback | Keeps recall without Google/Bing, which block datacentre IPs and need residential proxies |
| Cadence | Per signal kind, implemented as a cache TTL | Funding and news stay fast; the rest need not be re-asked every six hours |
| Where it runs | Its own small VM, separate from the Reacher box | Scraping egress can never affect email verification, which billing depends on |
| Where the cache lives | The platform database, not the service | Survives a VM rebuild, shared by API and worker, inspectable when a rep asks why an account showed nothing |

## Architecture

### `nexus-fetch` — a stateless service

New service in the repo under `services/fetch/`: FastAPI plus `scrapling[fetchers]` (Scrapling
0.4.15, BSD-3, Python 3.10–3.13), deployed as a container on its own VM.

| Endpoint | Request | Response |
|---|---|---|
| `POST /fetch` | `url`, `mode` (`http`\|`stealth`), `timeout_s` | `status`, `final_url`, `html`, `text`, `title`, `fetched_at` |
| `POST /search` | `query`, `limit`, `recency_days`, optional `engine` | list of `title`, `url`, `snippet`, `published_at` |
| `GET /health` | — | per-engine state, browser availability, queue depth |

`mode=http` uses Scrapling's `Fetcher` (curl_cffi impersonation, no browser). `mode=stealth` uses
`StealthyFetcher` for Cloudflare-fronted or JavaScript-rendered pages. Results are parsed with
Scrapling's own selectors, so a layout change is a selector fix rather than a regex rewrite.

It holds **no state**, so it can be destroyed and recreated at any time. It enforces per-host
pacing, a global concurrency cap, and a per-engine circuit breaker.

**A refusal is reported as `blocked`, never as an empty result.** "The engine refused" and "the
market is quiet" must not look alike — the same distinction `signal_source_runs` already draws
between `empty` and `ok`, and the reason a source that finds nothing every time is detectable.

### App-side adapters

- `SelfHostedSearchProvider` in `nexus/integrations/search/engines.py`, `query_dialect = "plain"`
  so the dorks render their phrase forms (operator dorks return nothing on these engines — measured,
  and already documented for DuckDuckGo).
- `nexus/fetching/client.py` — `fetch_page(url, mode)` for the first-party sources
  (`WebsiteWatchSignalSource`, ATS careers-page discovery, RSS).
- Resolution order for signals: **self-hosted → Firecrawl on failure or open breaker → in-process
  DuckDuckGo as the degraded floor.** Collection never stops, matching every other provider seam
  here.

### `web_cache` — migration `0057`

Platform-global: **no `tenant_id`**, so `scripts/apply_rls.py` leaves it alone and every read and
write goes through `get_platform_sessionmaker()`, exactly like `companies` and `people`. Enrolling
it in RLS would make the shared reader see zero rows — silent, not an error.

| Column | Purpose |
|---|---|
| `id` | `sha256` of the normalized query or URL plus engine and limit |
| `kind` | `search` or `page` |
| `subject` | the normalized query or URL, for operators reading the table |
| `engine` | which backend answered (`nexusfetch:ddg`, `firecrawl`, …) |
| `payload` | parsed hits or page text, JSON |
| `fetched_at`, `expires_at` | freshness; `expires_at` is indexed for the prune sweep |
| `hit_count`, `bytes` | so the saving is measurable rather than asserted |

A worker sweep deletes expired rows. Cache content is third-party text: it is data, never
instructions, and it flows through `clean_feed_text` on the way to a signal body as it does today.

**A cache hit is metered identically to a miss**, carrying `attrs.cached`, mirroring
`nexus/people/enrich.py` and `SourceDatabaseProvider`. The customer is charged for the answer; what
the cache improves is COGS. Billing only on a miss would make revenue depend on crawl ordering.

### Per-kind cadence, with no scheduler change

The TTL **is** the cadence:

| Signal kind | TTL | Rationale |
|---|---|---|
| `funding`, `news` | 6h | Being first is the value |
| `job_posting`, `hiring` | 24h | The ATS board is first-party and free, and postings do not turn over hourly |
| `tech_install`, website change | 24h | Slow-moving by nature |
| page fetches | 24h | Matches the website-watch baseline |

Every TTL is a runtime setting, so a cadence can be tightened without a deploy. 24h is the approved
starting point for everything except funding and news.

A dork whose cached entry is still fresh short-circuits before any request. `next_refresh_at`,
`tiering.classify` and the batch claim are untouched. Cached hits still run through classification
and dedupe, so an account's timeline is unchanged — the answer is simply not re-bought.

## Data flow for one account crawl

1. `pipeline.process_account` runs the sources concurrently, as today.
2. `DorkedSearchSource` renders up to four dorks. For each: look up `web_cache`; on a hit, use it;
   on a miss, call `nexus-fetch` `/search`, store the result; on failure or an open breaker, fall
   back to Firecrawl and store that, recording which engine answered.
3. First-party sources (ATS, public APIs, RSS, website watch) fetch through `nexus-fetch` when it is
   reachable — `stealth` only where a page needs it — and through the existing in-process httpx path
   when it is not.
4. Classification, `event_dedupe_key`, ingestion, alerting: unchanged.

## Failure posture

- `nexus-fetch` unreachable → breaker opens → Firecrawl → in-process DuckDuckGo. Signal collection
  degrades, never stops.
- A blocked engine records `error` on `signal_source_runs`, not `empty`.
- A `web_cache` read that raises is treated as a miss. A cache must never take down collection.
- Health: a `web fetcher` row on Platform health plus a non-blocking boot warning, mirroring
  `check_email_verifier` / `EMAIL VERIFIER UNREACHABLE`.

## Security

- **Not publicly reachable.** Firewalled to the app's egress addresses, shared secret
  (`NEXUS_FETCH_TOKEN`) on every call. An open "fetch any URL" endpoint is an open proxy and an
  SSRF primitive — the argument `nexus/sources/safety.py` already makes about DSNs.
- Refuses private, loopback, link-local and metadata hosts, reusing `_is_blocked_host`'s rules.
- No credentialed fetching, no login walls, no LinkedIn (no compliant API; the repo already reaches
  LinkedIn job pages only through a search index).
- `fetch_service_url` joins the runtime panel with a URL validator like `email_verify_url`.
  `NEXUS_FETCH_TOKEN` is `FORBIDDEN` there, because the panel returns values in plaintext — the same
  rule as `email_verify_auth_header`.
- Per-host pacing and a global cap, so a bug in the app cannot melt the service or the targets.

## Billing

Prices do not move. Costs do, and a cost is an observation: record it through
`PUT /admin/billing/costs/{id}` so the margin floor validates against the true number.
`signal.news_scan` falls from $0.0064 towards VM amortisation. Nothing lands under the floor, since
costs only fall, but the endpoint's work list is still the thing to read after each change.

## Rollout

1. **Shadow.** Self-hosted runs beside Firecrawl, both recorded, Firecrawl's results used. Reports,
   never repairs, like `companies/diff.py`. Read asymmetrically: hits only Firecrawl found are the
   failure.
2. **Cache on, Firecrawl still primary.** Measures the real repeat rate and the real saving with no
   recall risk, and answers "what is our actual monthly volume" from `signal.news_scan` usage events
   rather than from the rate card.
3. **Self-hosted primary**, paid fallback, promoted per engine on measured agreement.
4. **Firecrawl off for signals**, key retained for fallback.

## Cost

VM roughly $6–12/month. Against ~$3,070/month of dork spend at 1,000 hot accounts, the cache and
per-kind cadence alone remove roughly 60% of queries and self-hosting takes most of the remainder.
Phase 2 replaces these estimates with measured figures before Phase 3 changes any default.

## Testing

- The offline suite stays offline: the fetch client is injectable, like
  `network/connectors/fixture.py`. No test reaches the network.
- New tests pin what would otherwise rot silently:
  - a cache hit issues no HTTP request and is still metered, with `attrs.cached`;
  - a blocked engine records `error`, not `empty`;
  - the SSRF guard refuses private, loopback and metadata hosts;
  - the fallback order self-hosted → Firecrawl → DuckDuckGo;
  - per-kind TTLs are honoured;
  - `NEXUS_FETCH_TOKEN` appears in no response model and in no runtime-panel value.
- Staging validation compares signals found per account before and after, the same asymmetric read
  as the shared-company diff.

## Out of scope

- **Enrichment and research** (`enrich.account`, `ai.research_brief`, `ai.account_qa`,
  `ai.icp_from_website`): the Exa searches behind blank accounts and briefs. Own spec, next.
- **Discovery and lookalikes** (`search_companies`, `find_similar`) and the Apify actors: Exa's
  neural search has no self-hosted equivalent without building an index. Own spec, last.
- Residential proxies and Google/Bing scraping: excluded by decision.
