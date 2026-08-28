# 06 — Scaling: what to change, in what order

The current shape is deliberately the smallest thing that is still production-correct. Nothing here
is an architectural dead end — every stage is a config change, not a rewrite.

## Stage 0 — today (10–15 users, ~$70–105/mo)

| Component | Setting | Est. |
|---|---|---|
| App | 1–3 replicas, 0.5 vCPU / 1 GiB, `--workers 1` | $10–35 |
| Worker | 1 replica, 0.25 vCPU | $15–20 |
| Valkey | 1 replica, 0.25 vCPU | ~$10 |
| Postgres | B1ms, 32 GB, no HA | ~$17 |
| ACR Basic + Log Analytics | | ~$8 |

---

## The one number that governs everything: connections

**B1ms allows roughly 50 connections.** Peak usage is:

```
app_replicas × processes × (pool + overflow + platform_pool + platform_overflow)
  ... DOUBLED during a rollout (old and new revisions serve simultaneously)
  ... + the worker's single process
```

At the configured 5+5 (+2+3 platform) = **15 per process**, `--workers 1`:

| Configuration | Steady | During deploy | Fits ~50? |
|---|---|---|---|
| 1 app + 1 worker | 30 | **45** | ✅ tight |
| 2 app + 1 worker | 45 | **75** | ❌ |

**The deploy column is the one people miss.** It works fine, then breaks during a release — which
is also the worst moment to debug it. Exceeding `max_connections` does not degrade gracefully:
Postgres refuses new connections and it surfaces as 500s under exactly the load that caused it.

> `app_min` is **not an independent knob**. Raising it to 2 requires either a bigger Postgres SKU or
> a smaller pool. Change them together or not at all.

The pool is env-driven (`NEXUS_DB_POOL_SIZE`, `NEXUS_DB_MAX_OVERFLOW`), so it is the **first** dial
to reach for — before upsizing the database, before reducing replicas.

---

## Stage 1 — 50–200 users (~$150–200/mo)

**Triggers:** p95 latency > 1s, or CPU sustained > 70%, or you want no single point of failure.

```hcl
pg_sku  = "B_Standard_B2s"   # ~100 connections
app_min = 2
app_max = 6
```

Two replicas remove the single point of failure — with `app_min = 1`, a crash is a full outage for
the tens of seconds it takes to restart *and re-run migrations on boot*.

New budget: 2 app + 1 worker = 45 steady, 75 during deploy — fits B2s, not B1ms. **This is why the
SKU and the replica count move together.**

## Stage 2 — 200–1,000 users (~$400–600/mo)

**Triggers:** DB CPU > 70% sustained, connections > 70% of max, storage IOPS throttling.

```hcl
pg_sku        = "GP_Standard_D2ds_v5"
pg_ha_enabled = true          # zone-redundant standby; roughly doubles DB cost
app_min       = 2
app_max       = 10
```

Also now worth doing:

- **Scale the worker horizontally.** `nexus/workers/scheduler.py` takes a **Postgres advisory
  lock** per heartbeat, so exactly one worker in a fleet enqueues the recurring drivers. The
  `max_replicas = 1` pin is conservatism, not a constraint — the duplicate-scheduled-job problem is
  already solved in code. Raise it and add a KEDA queue-depth rule.
- **Move Valkey to Azure Managed Redis** once losing in-flight jobs on restart stops being
  acceptable, or queue depth is routinely non-trivial.
- **Bump `NEXUS_DB_POOL_SIZE`** back toward 10/20 now that the SKU allows it.

## Stage 3 — 1,000–10,000 users

- **Read replicas** for analytics and reporting queries
- **Front Door + WAF** for global routing, caching and edge TLS — not before real traffic justifies it
- **Split the worker by workload class** (ingestion vs. campaigns vs. AI) so a slow AI job cannot
  starve signal collection
- **Partition `usage_events`** — `scripts/partition_usage_events.sql` exists for this
- **Redis becomes genuinely load-bearing** — managed, replicated, non-negotiable

## Stage 4 — 10,000+

Multi-region, tenant sharding, dedicated AI worker pools. Do not design for this now. The property
worth preserving is that nothing above requires rewriting application code.

---

## The measured bottleneck, and it is not the database

From `deploy/loadtest/README.md`: 500 tenants × 1,000 accounts demands **23.15 accounts/sec**
against a measured drain of **0.036/sec** on one serial worker.

Two mitigations already shipped:

- **Tiered refresh** (`accounts.next_refresh_at`, migration 0042) — hot accounts refresh often, cold
  ones rarely. Storing the due-time made the claim query an index scan that stops at the limit:
  **489ms → 5–8ms warm**, O(batch) instead of O(estate).
- **Concurrent sources** — per-account crawl **26.98s → 14.94s (1.81×)** measured over 355 real
  crawls.

**Still open:** `run_worker` is strictly serial (measured effective concurrency 0.99) with one
replica. ~15.65s per account is 0.064/s against 5.11/s demand at a 15% hot ratio.

The obvious next lever is bounded in-flight concurrency — `process_account` is ~99%
await-on-network. **But it must be capped by the DB pool**, since each in-flight job holds a
session. Add concurrency without raising the pool and you convert a throughput problem into
connection exhaustion.

---

## How to know when to scale

```bash
# DB CPU and connections
az monitor metrics list --resource "$(az postgres flexible-server show -n nexus-prod-pg-v3 \
  -g nexus-prod-rg --query id -o tsv)" \
  --metric cpu_percent active_connections --interval PT5M --output table | tail -20

# App replica count over time
az monitor metrics list --resource "$(az containerapp show -n nexus-prod-app \
  -g nexus-prod-rg --query id -o tsv)" \
  --metric Replicas --interval PT15M --output table | tail -10
```

| Signal | Threshold | Action |
|---|---|---|
| DB CPU | > 70% for 1h | Next Postgres SKU |
| DB connections | > 70% of max | Lower pool first, then SKU |
| App replicas at max | sustained | Raise `app_max` (check connection budget) |
| p95 latency | > 1s | Profile before scaling — usually a query, not capacity |
| Queue depth | consistently > 0 | Scale the worker |
| Storage | > 80% | Grow it (one-way — storage cannot shrink) |

**Investigate software before buying hardware.** N+1 queries, a missing index, or an unbounded
external call are all cheaper to fix than a SKU upgrade, and scaling around them just makes the
same bug more expensive.
