# 03 — Metering Architecture

> Append-only, idempotent, tenant-attributed usage recording for every capability — the single
> source of truth that entitlements, invoices, margins, and CS analytics are all derived from.
>
> Related: [01-Billing-Architecture](01-Billing-Architecture.md) ·
> [10-Usage-Tracking](10-Usage-Tracking.md) · [12-Cost-Analysis](12-Cost-Analysis.md)

## 1. The usage event

```
billing_usage_events (TenantScoped ⇒ RLS; PARTITION BY RANGE (occurred_at), monthly)
  id                 uuid-hex PK
  tenant_id          str(32)  idx
  capability_id      str(80)  idx          -- catalog ID, e.g. "ai.email_draft"
  quantity           numeric(18,6)         -- units in the capability's unit (1 draft, 1834 tokens, 0.3 GB…)
  unit               str(20)               -- denormalized from catalog: action|token|search|seat|gb|minute|credit
  user_id            str(32) null idx      -- actor attribution when a human triggered it
  source             enum: api|worker|middleware|system
  idempotency_key    str(120) UNIQUE(tenant_id, idempotency_key)   -- replay-safe
  attrs              jsonb                 -- tokens_in/out, provider, model, account_id, run_id…
  unit_cost_usd      numeric(12,8) null    -- stamped at write from billing_cost_rates (COGS truth-in-time)
  billed_credits     numeric(12,4) null    -- stamped by rating (null until rated / if included)
  occurred_at        timestamptz idx
  recorded_at        timestamptz default now()
```

Design notes:
- **Idempotency key is mandatory** for worker/system events (retries are routine in the queue)
  and auto-derived (`request_id`) for API events. Uses the same key discipline as
  `nexus/core/idempotency.py` — a replayed job can never double-bill.
- **COGS stamped at write** (`unit_cost_usd` from the cost-rate table, versioned) so margin
  reports reflect the cost *at the time of use*, immune to later price changes.
- **Monthly partitions + a retention job**: raw events kept 13 months hot, archived to object
  storage after; rollups kept forever. At 1M customers this is the table that matters —
  partitioning is day-one schema, not a retrofit.

## 2. Ingestion pipeline

```
check_and_meter()                       (allow path)
  ├─ Valkey INCRBY hot counters                     (sync, ~0.2ms, best-effort)
  └─ EventBus.publish("usage.recorded", payload)    (fire-and-forget)
        └─ queue job record_usage (existing worker) → INSERT billing_usage_events
              └─ on conflict (idempotency) DO NOTHING

hourly worker job rollup_usage:
  billing_usage_events → billing_usage_rollups (tenant, capability, hour, day:
      SUM(quantity), SUM(unit_cost_usd), SUM(billed_credits), COUNT)
  watermark-based (last_rolled_at), idempotent, tenant-parallel

reconcile_counters (daily): Valkey counters ⇐ rollups (drift self-heals)
```

Why this shape: it reuses the exact machinery already in production here — the in-process
EventBus, the Redis/Valkey queue with the crash-safe worker loop, watermark-style periodic
drivers (`refresh_due_accounts` pattern), and per-tenant sessions for RLS-safe writes. No new
infrastructure. At larger scale the EventBus hop swaps for a Valkey stream with zero call-site
changes (the emit API is the seam).

## 3. What gets metered (complete map: [09-Billable-Resources](09-Billable-Resources.md))

| Class | Emit point | Examples |
|---|---|---|
| Every API request | ASGI middleware (extends `RequestContextMiddleware`) | `api.request` w/ route, status, ms |
| Every LLM prompt+response | `nexus/agents/llm.py` chokepoint | tokens in/out, model, purpose (research, qa, messaging, scoring, call_script, icp_chat…) |
| Every search | search provider registry (`integrations/search`) | Exa/Brave/Serper/DDG calls from discovery, lookalikes, news, QA |
| Every crawl | browser/enrichment providers | account enrich, person research |
| Every verification | verification provider | Reacher checks, DNS checks, reverify sweeps |
| Every email sent / draft saved | `integrations/email_sender` | campaign/cadence/approval sends, IMAP drafts |
| Every notification | alert dispatcher | in-app, webhook, Slack, email digest |
| Every background job | worker `@metered` decorator | ICP discovery run, account refresh, CRM sync, network sync, cadence tick |
| Every workflow execution | orchestration engine step loop | run started, per-step tool execution |
| Every import/export | CSV endpoints | custom-fields import, LinkedIn import, (future) exports |
| Every integration push | CRM/SEP connectors | HubSpot/Salesforce upserts, Outreach/Salesloft pushes |
| Every storage unit | nightly `measure_storage` job | contacts, accounts, signals, network persons (rows→GB) |
| Every seat / workspace | membership + workspace mutations | seat add/remove events + daily snapshot |
| Every telephony minute | telephony provider (when enabled) | call minutes, recordings |

Frontend click-level telemetry is **analytics, not billing** (see
[10-Usage-Tracking](10-Usage-Tracking.md)) — billing meters the server-side action the click
triggers, which is the only defensible number on an invoice.

## 4. Query surfaces

- `GET /billing/usage` (tenant-facing): current period per-capability usage vs. quota — powers
  the in-app usage page and upgrade prompts.
- `GET /admin/billing/usage` (staff): any tenant, any window, event-level drill-down.
- Rollups feed the revenue/cost/margin dashboards ([06-Admin-Portal](06-Admin-Portal.md)) and the
  rating engine at period close ([04-Pricing-Engine](04-Pricing-Engine.md)).

## 5. Integrity guarantees

1. **No double-billing:** unique (tenant, idempotency_key).
2. **No lost usage:** if the queue is down, `check_and_meter` falls back to synchronous insert in
   the request transaction (slower, never silent loss); worker-side emits are inside the job's
   own tenant session/commit.
3. **No cross-tenant leakage:** TenantScoped + RLS, same guarantees as every other table.
4. **Reconcilable:** events ↔ rollups ↔ invoice lines all carry the same capability IDs and
   period keys; a `reconcile_invoice` admin tool re-rates any period from raw events.
5. **Auditable:** events are never updated or deleted inside the retention window; corrections
   are compensating events (`quantity < 0`, reason-coded).
