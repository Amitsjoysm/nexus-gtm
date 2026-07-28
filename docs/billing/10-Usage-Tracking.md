# 10 — Usage Tracking

> How usage is captured end-to-end, what the customer sees, what CS sees, and where
> click-level telemetry fits.
>
> Related: [03-Metering-Architecture](03-Metering-Architecture.md) ·
> [06-Admin-Portal](06-Admin-Portal.md)

## 1. Capture layers

| Layer | Mechanism | Granularity |
|---|---|---|
| API | middleware (extends `RequestContextMiddleware`, which already assigns request IDs) | every request: route template, status, duration, tenant, user |
| Feature | `Metered(...)` dependency on spend-bearing endpoints | capability-level with domain attrs (account_id, run_id…) |
| AI | `nexus/agents/llm.py` chokepoint | every prompt/response: tokens in/out, model, provider, purpose |
| Workers | `@metered` decorator on handlers | every job: name, tenant, duration, outcome |
| Providers | search/browser/verify/CRM/SEP/email registries | every external call: provider, latency, success |
| Storage | nightly gauge job | GB per tenant per store |
| Frontend | (phase 3) `POST /telemetry/events` batch endpoint | screen views, feature clicks — **analytics only, never invoiced** |

Attribution on every event: `tenant_id` (always), `user_id` (when a human acted),
`occurred_at`, `source`, idempotency key. Timestamps are UTC (`utcnow` house-wide).

## 2. Customer-facing usage (drives self-serve expansion)

`/settings → Billing → Usage` (tenant surface, [05](05-Subscription-System.md) §6):
- per-capability meter bars: used / included / overage-to-date, reset date;
- credit balance + burn history (ledger view);
- soft-limit banners reuse the existing Alert system (`in_app` channel) — the same plumbing that
  shows "ICP discovery paused" today;
- 402 responses carry machine-readable `{capability, used, quota, reset_at, upgrade_url}` so the
  SPA can render inline upgrade prompts at the moment of denial.

## 3. CS / growth views (Admin)

- **Upgrade pipeline:** tenants ranked by (soft-limit hits × denial events × credit purchases)
  in the last 30 days.
- **Adoption:** capability usage heatmap per tenant vs. plan cohort median — flags unused paid
  modules (churn risk) and hot free-tier users (expansion).
- **Anomaly watch:** day-over-day usage spikes > Nσ per capability → ops alert (abuse/runaway
  automation detection; also protects COGS).

## 4. Privacy & retention

- Usage events carry IDs, never content (no email bodies, no prompts — `attrs` is whitelisted
  keys only). Prompt/response content stays where it already lives (runs, approvals).
- Raw events: 13 months hot → archive; rollups: indefinite; frontend telemetry: 90 days.
- Tenant deletion cascades usage events; invoices are retained per legal policy in anonymized
  form (tenant_id kept, no PII in billing lines by design).
