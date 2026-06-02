# NEXUS GTM — Architecture

> AI-powered Go-To-Market intelligence platform (Pocus-class). Multi-tenant SaaS that gives
> every sales rep an AI co-pilot backed by unified account intelligence. The differentiator is
> the **Relevance Engine**: every AI output is conditioned on the tenant's ICP, value props, and
> product context, making outputs prescriptive instead of generic.

## 1. Design goals

1. **Multi-tenant by construction** — no query may cross a tenant boundary. Enforced in two layers:
   app-level `TenantSession` guard (works everywhere, incl. SQLite tests) and Postgres
   Row-Level Security (defense in depth in prod).
2. **Few external dependencies** — core runs on FastAPI + SQLAlchemy + Pydantic only. Scraping,
   browser automation, and LLM inference sit behind interfaces with swappable adapters. The whole
   system runs end-to-end with **zero external services** (SQLite + in-memory queue + stub LLM).
3. **Everything swappable** — `LLMProvider`, `BrowserProvider`, `EnrichmentProvider`,
   `SignalSource`, `CRMConnector`, `SEPConnector`, `TaskQueue` are all interfaces. Adapters
   (Scrapling, DuckDuckGo, Cloak CDP, Hermes, Salesforce, HubSpot, Redis) are leaf modules.
4. **Async-first** — all I/O is async; agent/enrichment/ingestion work runs on a task queue.

## 2. Subsystems

| Subsystem | Package | Responsibility |
|---|---|---|
| Platform core | `nexus/core` | config, db, tenancy, auth, RBAC, event bus |
| Relevance Engine ★ | `nexus/relevance` | per-tenant ICP / value-props / product context + retrieval |
| Ingestion | `nexus/ingestion` | signal sources, signal library, CRM sync → `SignalEvent` |
| Enrichment | `nexus/enrichment` | waterfall email/phone via Scrapling + DuckDuckGo fallback |
| Agents (AI core) | `nexus/agents` | runtime + Research / Scoring / Messaging / ContactRec / QA |
| Rep UX | `nexus/inbox`, `nexus/lists` | intelligent inbox, list builder |
| Automation | `nexus/plays` | signal → action plays, alerts, compelling events |
| Integrations | `nexus/integrations` | CRM + SEP push |
| Analytics | `nexus/analytics` | performance dashboards |
| API / workers | `nexus/api`, `nexus/workers` | FastAPI routers, queue consumers |

## 3. Multi-tenancy model

```
Tenant (company / customer)
 └── Workspace (team)
      └── Membership(user, role)         role ∈ {owner, admin, manager, rep}
User (global identity, may belong to many tenants)
```

Every tenant-scoped table has a non-null `tenant_id`. Access is mediated by `TenantSession`,
a thin wrapper over `AsyncSession` that auto-injects `tenant_id` filters and stamps `tenant_id`
on inserts. The request pipeline resolves the tenant from the JWT and binds a `TenantContext`
(contextvar) for the lifetime of the request. In Postgres, `SET LOCAL app.tenant_id` + RLS
policies provide a second, database-enforced guarantee.

## 4. Relevance Engine (the spine)

`RelevanceProfile` holds, per tenant:
- **ICP** — firmographic rules (industry, size band, geo, tech) + weights.
- **Value propositions** — list of `{name, description, pains_solved}`.
- **Product context** — what we sell, differentiators, proof points.

`RelevanceEngine` exposes:
- `score_icp_fit(account)` → deterministic 0–100 ICP-fit from the profile rules (no LLM needed).
- `build_context(account, contacts, signals)` → a compact `RelevanceContext` injected into every
  agent prompt so outputs are grounded in *this* tenant's GTM motion.

## 5. Agent layer

```
AgentRuntime
  ├─ LLMProvider            (StubLLMProvider | OpenAICompatProvider | HermesAdapter)
  ├─ RelevanceEngine        (context injection)
  └─ agents/
       ResearchAgent        account + person research (uses BrowserProvider + DDG)
       ScoringAgent         ICP fit / intent / health (deterministic + LLM rationale)
       MessagingAgent       personalized outreach grounded in value props
       ContactRecAgent      who to contact + why
       QAAgent              "ask anything about an account"
```

Each agent: takes a typed input, builds a relevance-grounded prompt, calls `LLMProvider`,
returns a typed result, and records an `AgentRun` (inputs, output, tokens, latency) for audit.
The `StubLLMProvider` returns deterministic, template-based outputs so the whole pipeline runs
and is testable with no API key.

## 6. Data flow (account intelligence loop)

```
Ingestion.sources ─▶ SignalEvent ─▶ Inbox.prioritizer ─▶ InboxTask
        │                              ▲
        ▼                              │
   Enrichment.waterfall ─▶ Contact ────┘
        │
        ▼
   Agents (Research→Scoring→Messaging) ──▶ AccountScore + suggested message
        │
        ▼
   Plays.engine (signal+score thresholds) ──▶ actions (alert / SEP push / create task)
```

## 7. Task queue

`TaskQueue` interface with two adapters: `InMemoryTaskQueue` (default, for dev/test/single-node)
and `RedisTaskQueue` (prod, multi-worker). Jobs: `run_ingestion`, `enrich_account`, `run_agent`,
`evaluate_plays`. Workers are stateless consumers.

## 8. Error handling & resilience

- Adapters never raise across the interface boundary uncaught; they return typed results with a
  `source`/`confidence`/`error` field. The waterfall enricher tries providers in order until one
  yields a confident hit, else returns "no match" (never throws).
- Agent failures are captured on the `AgentRun` (status=failed) and surfaced, not swallowed.
- All external calls have timeouts; the LLM/browser adapters are the only network boundaries.

## 9. Testing strategy

Runs on SQLite + in-memory queue + stub LLM, so the suite needs no services:
- `test_tenancy` — cross-tenant reads/writes are impossible.
- `test_relevance` — ICP scoring + context build.
- `test_scoring`, `test_agents` — agent runtime with stub LLM.
- `test_enrichment` — waterfall ordering + fallback.
- `test_inbox` — prioritization ranking.
- `test_api` — auth → create account → run agent → read inbox, over HTTP.

## 10. Production posture (Optimizer notes)

Postgres + RLS, Redis queue, connection pooling, prepared-statement caching, per-tenant rate
limits, structured logging + Prometheus metrics, response caching for relevance context,
idempotent ingestion (dedupe key), and outbox pattern for integration pushes.
