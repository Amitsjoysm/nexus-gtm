# 02 — Entitlement Engine

> One engine answers "may this tenant use this capability right now, and at what price."
> Application code never branches on plan — it asks the engine.
>
> Related: [01-Billing-Architecture](01-Billing-Architecture.md) ·
> [03-Metering-Architecture](03-Metering-Architecture.md) ·
> [08-Feature-Catalog](08-Feature-Catalog.md) · [15-Migration-Strategy](15-Migration-Strategy.md)

## 1. Entitlement record shape

Every `billing_plan_entitlements` row (and every contract override) carries the full control
surface required by the brief:

| Field | Type | Meaning |
|---|---|---|
| `capability_id` | FK → catalog | e.g. `ai.email_draft` (Feature ID) |
| `category` / `sub_category` | from catalog | `ai` / `outreach` (denormalized for admin filters) |
| `mode` | enum | `enabled` · `disabled` · `trial` · `metered` · `unlimited` |
| `quota` | int/null | included units per reset period (null = n/a) |
| `soft_limit_pct` | int | warn threshold (default 80) → alert + banner, never block |
| `hard_limit` | int/null | absolute stop (null = overage allowed instead) |
| `reset_policy` | enum | `monthly_anniversary` · `calendar_month` · `daily` · `never` |
| `burst_limit` | int/null | max units per rolling minute (protects COGS spikes) |
| `rate_limit` | str/null | `"60/min"`-style; enforced at the same seam |
| `cooldown_s` | int/null | min seconds between uses (e.g. re-verify email: 30d ⇒ 2592000 — subsumes today's hardcoded `email_reverify_cooldown_days`) |
| `overage_price_credits` | numeric/null | per-unit price beyond quota (null = block at quota) |
| `feature_flag` | str/null | optional flag name gating rollout independent of plan |
| `depends_on` | json list | capability IDs that must be enabled (e.g. `ai.call_script` ⇒ `module.calling`) |
| `trial_quota` / `trial_days` | int/null | trial-mode allowance |

The **catalog row** ([08](08-Feature-Catalog.md)) supplies immutable metadata (name, description,
unit, category, default mode, meter kind); the **entitlement row** supplies per-plan policy.

## 2. Resolution order

For tenant T, capability C:

```
contract override (billing_contracts.entitlements[C])      # enterprise custom
  → plan entitlement (subscription.plan × C)
    → plan-class default (e.g. class=unlimited ⇒ everything unlimited)
      → catalog default_mode (shadow-allow for unregistered/new capabilities)
```

First match wins. The resolved set is compiled to a flat dict and cached in Valkey under
`bill:ent:{tenant_id}:{version}`; `version` is bumped on any plan/contract/coupon mutation, so
invalidation is O(1) and there is no stale-TTL window. Offline/dev falls back to an in-process
LRU (same degrade pattern as the relevance cache).

## 3. Decision outcomes

`check_and_meter()` returns one of:

| Outcome | Condition | Product behavior |
|---|---|---|
| `allow` | within quota / unlimited | proceed, meter |
| `allow_overage` | quota exhausted, overage priced | proceed, meter at overage price, burn credits |
| `warn` | crossed soft limit | proceed + emit `usage.soft_limit` event (in-app alert via existing Alert system, channel `in_app`) |
| `block_quota` | hard limit reached | HTTP 402 payload: capability, usage, reset date, upgrade CTA |
| `block_disabled` | mode=disabled for plan | 402 with "not in your plan" + upsell metadata |
| `block_dependency` | dependency disabled | 402 naming the missing module |
| `throttle` | burst/rate/cooldown hit | HTTP 429 with `Retry-After` |

All block outcomes are themselves metered (`billing.denied` events) — denial telemetry is the
single best expansion-signal feed for Customer Success ([06-Admin-Portal](06-Admin-Portal.md)).

## 4. Counters

- **Fast path:** Valkey `INCRBY bill:cnt:{tenant}:{capability}:{period}` (same Valkey the queue
  already uses). Used for soft limits, burst, and rate windows.
- **Authoritative path:** the metering rollups ([03](03-Metering-Architecture.md)). Hard-limit
  checks read the rollup + today's events; a Valkey wipe can therefore never grant free quota.
- **Reset** is computed, not scheduled: period keys embed the reset boundary
  (`2026-08` or anniversary window), so "resetting" costs nothing.
- **Seats** are a synchronous counter (count of memberships) checked at invite time via the same
  seam (`seat.member`, quantity = current+1).

## 5. Enforcement placement in the existing codebase (implementation phase)

No per-feature branching — three generic hooks:

1. **Routers:** `Depends(Metered("cap.id"))` added to the ~20 spend-bearing endpoints
   (agents run, orchestration runs/chat, lookalikes, source-contacts, enrich, reverify,
   network search/sync/import, campaigns, cadence approve, calling script, export/import,
   relevance AI-ICP). Mechanical, additive.
2. **Worker:** `@metered(...)` on the ~10 spend-bearing handlers in `nexus/workers/tasks.py`
   (`discover_icp_accounts`, `process_account`, `run_campaign`, `advance_cadences`,
   `sync_network_account`, `sync_crm_*`, digests).
3. **LLM chokepoint:** one emit inside `nexus/agents/llm.py` complete() captures every token of
   every provider with purpose attribution (`purpose=` is already threaded through every call).

Everything else (plain CRUD reads) is covered by blanket `api.request` middleware metering —
recorded for analytics, unlimited on every paid plan by default.

## 6. Backward compatibility invariants

- Unregistered capability ⇒ `allow` + shadow log. Shipping the engine changes **zero** behavior.
- Every existing tenant is auto-assigned the `legacy-unlimited` plan (class `unlimited`) at
  migration time ([15-Migration-Strategy](15-Migration-Strategy.md)); enforcement only begins when
  an admin moves a tenant to a priced plan.
- `NEXUS_BILLING_ENFORCEMENT=off|shadow|on` is the global kill switch (env + admin flag), default
  `shadow` in the first production release.
