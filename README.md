# NEXUS GTM

AI-powered Go-To-Market intelligence platform — a multi-tenant SaaS MVP in the spirit of
[Pocus](https://pocus.com). NEXUS ingests buying signals, scores accounts against each tenant's
**Relevance Engine** (ICP + value props + product context), and turns the result into a prioritized
rep inbox, AI research/messaging, waterfall enrichment, list building, and signal-triggered plays.

The whole platform **runs with zero external dependencies** — SQLite, an in-memory queue, and a
deterministic stub LLM — so you can clone and run it offline. Production adapters (Postgres, Redis,
a real LLM gateway, Scrapling/CloakBrowser scraping) swap in behind interfaces with no code changes.

## Quick start

```bash
pip install -e ".[dev]"          # core + test tooling, all pure-Python
uvicorn nexus.main:app --reload  # API + rep UI at http://127.0.0.1:8000
```

Open http://127.0.0.1:8000 — sign up, click **Seed demo account**, and watch the pipeline produce
signals, a score, and a prioritized inbox. Interactive API docs are at `/docs`.

Run the background worker (optional — the API also runs the pipeline synchronously):

```bash
python -m nexus.workers.worker
```

## Architecture at a glance

```
ingestion → Relevance Engine → scoring → inbox / plays → rep UI + API
   sources      (ICP/value props)   agents    automation
```

- **Multi-tenancy** (`nexus/core/tenancy.py`) — two layers of defense: an application-level
  `TenantSession` that stamps/filters/validates every read and write, plus Postgres Row-Level
  Security in production. A `before_flush` listener is the backstop that refuses to persist any
  tenant-scoped row that doesn't match the active tenant.
- **Relevance Engine** (`nexus/relevance/engine.py`) — deterministic ICP-fit scoring (no LLM) and a
  compact context string injected into every agent prompt so outputs are prescriptive, not generic.
- **Agents** (`nexus/agents/`) — `research`, `scoring`, `messaging`, `contact_rec`, `qa`. Each runs
  through a shared runtime that loads context, records an audit trail (`AgentRun`), and never crashes
  the request. The `LLMProvider` is pluggable: a deterministic stub by default, OpenAI-compatible
  (e.g. a Hermes gateway) in production.
- **Enrichment** (`nexus/enrichment/`) — a waterfall over pluggable providers (search-based via the
  browser adapter, pattern-based fallback) that merges the highest-confidence value per field.
  Browser adapters: Scrapling → CloakBrowser CDP → DuckDuckGo fallback.
- **Automation** (`nexus/inbox`, `nexus/lists`, `nexus/plays`, `nexus/integrations`) — prioritized
  inbox, firmographic list builder, and a declarative plays engine (signal → create_task / alert /
  sep_push).
- **Workers** (`nexus/workers/`) — a `TaskQueue` abstraction with in-memory and Redis backends.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

## Configuration

All settings use the `NEXUS_` env prefix (see [`.env.example`](.env.example)). Safe local defaults
mean you don't need to set anything to run. Key switches:

| Variable | Default | Production |
| --- | --- | --- |
| `NEXUS_DATABASE_URL` | `sqlite+aiosqlite:///./nexus.db` | `postgresql+asyncpg://…` |
| `NEXUS_QUEUE_BACKEND` | `memory` | `redis` |
| `NEXUS_LLM_PROVIDER` | `stub` | `openai_compat` (+ `NEXUS_LLM_BASE_URL`, `NEXUS_LLM_API_KEY`) |
| `NEXUS_BROWSER_PROVIDER` | `auto` | `scrapling` / `cloak` |
| `NEXUS_SECRET_KEY` | dev placeholder | **must be overridden** |

Install optional extras as needed: `pip install -e ".[postgres,redis,scraping]"`.

## API tour

```
POST /api/auth/signup                 provision a tenant + owner, returns a JWT
POST /api/auth/login                  exchange credentials for a JWT
GET  /api/relevance/profile           read the tenant GTM profile
PUT  /api/relevance/profile           define ICP / value props / product context
POST /api/accounts                    create an account   (GET lists, /{id} fetches)
POST /api/accounts/{id}/contacts      add a contact
POST /api/accounts/contacts/{id}/enrich   run the enrichment waterfall
GET  /api/agents                      list available agents
POST /api/agents/{name}/run           run one agent for an account
POST /api/agents/pipeline/{account}   run the full ingest→score→inbox→plays loop
GET  /api/inbox                       prioritized open tasks  (POST /{id}/complete)
POST /api/lists/preview | /api/lists  preview / build a prospect list
GET  /api/plays  | POST /api/plays    list / create automations
POST /api/ingest/{account}            pull signals from configured sources
GET  /api/analytics/overview          tenant aggregates
```

Every data endpoint is tenant-scoped via the JWT and RBAC-gated (roles: owner → admin → manager → rep).

## Tests

```bash
pytest                # 29 tests, fully offline
```

Coverage targets the load-bearing paths: tenant isolation, Relevance Engine scoring, the agent
runtime against the stub LLM, the enrichment waterfall, inbox prioritization, and an end-to-end API
flow including cross-tenant isolation.
