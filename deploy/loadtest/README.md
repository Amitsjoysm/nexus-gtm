# Load & Soak testing (O-3)

k6 scripts that establish a performance baseline and catch regressions before they reach users.

Two independent things are worth load-testing here, and they fail for different reasons:

* **The API read path** — what a rep hits all day. Measured by these k6 scripts.
* **The automation heartbeat** — the account-refresh pipeline. k6 cannot see it at all (it is
  off-request), and it is the one that does not scale. See "Heartbeat throughput" below.

## Run

k6 runs in a container — no local install needed. Join the compose network and address the app
service directly, which avoids the `host.docker.internal` hop and the broken Caddy TLS:

```bash
docker run --rm --network nexus-gtm_default -e BASE_URL=http://app:8000 -e LOADTEST_EMAIL=sdr@example.com -e LOADTEST_PASSWORD=... -v "$PWD/deploy/loadtest:/scripts" grafana/k6 run /scripts/load.js
```

```bash
docker run --rm --network nexus-gtm_default -e BASE_URL=http://app:8000 -e SOAK_VUS=30 -e SOAK_DURATION=30m -e LOADTEST_EMAIL=sdr@example.com -e LOADTEST_PASSWORD=... -v "$PWD/deploy/loadtest:/scripts" grafana/k6 run /scripts/soak.js
```

(On Windows PowerShell use `-v "${PWD}\deploy\loadtest:/scripts"`.)

### Auth: why these scripts log in rather than sign up

They used to call `/api/auth/signup` in `setup()`. That endpoint now returns **403 "Email
verification required"**, so `setup()` got no token and every authed read 401ed — reported as an
**80% error rate against the app** when the truth was a broken harness. `auth.js` logs in instead.

`/api/analytics/overview` is **manager+**; a `rep` token gets 403 on it. Supply
`LOADTEST_MGR_EMAIL` / `LOADTEST_MGR_PASSWORD` to include it, otherwise it is skipped rather than
counted as an error. An MFA-enrolled account fails fast with a clear message — its login returns a
challenge token that authorizes only `/auth/mfa/verify`, and quietly using that as a bearer token
would produce a run of 401s instead of a stop.

Access tokens expire after **1 hour**, so a soak longer than that needs a re-login inside the
iteration; today's scripts acquire the token once in `setup()`.

## Measured — 2026-08-04, single laptop, Docker Desktop

App, worker, Postgres, Valkey, Prometheus and Grafana all co-located under the Docker Desktop VM,
`nexus-gtm:latest` at commit `23212f8`. Read path = `/api/accounts?limit=50`,
`/api/signals?limit=50`, `/api/inbox`.

| VUs | p50 | p95 | errors | throughput |
|---|---|---|---|---|
| 10 | 22 ms | 240 ms | 0.00% | 14.2 req/s |
| 25 | 31 ms | **470 ms** | 0.00% | 33.9 req/s |
| 50 | 35 ms | 735 ms | 0.09% | 67.2 req/s |
| 100 | 304 ms | 3.52 s | **5.43%** | 87.2 req/s |
| 200 (ramp) | 412 ms | 4.47 s | 1.90% | 129 req/s |

**The knee is between 25 and 50 VUs**, and it is a connection-pool ceiling, not CPU:

* Every error above was an HTTP 500 raised by
  `asyncpg.exceptions.TooManyConnectionsError` — *"sorry, too many clients already"*.
* Sampled peak during the 200-VU ramp: **98 of 100** `max_connections` (90 app + 8 owner), with
  `superuser_reserved_connections = 3` leaving 97 usable.
* The pools are overcommitted **1.8× by construction**, independent of load. Per app process the
  engines allow `pool_size=10 + max_overflow=20` (app) plus `2 + 3` (platform) = **35**. The app
  runs `--workers 2` across `replicas: 2` = **4 processes = 140**, plus the worker's 35 = **175
  possible against 97 usable**.

So the first capacity lever for the read path is Postgres `max_connections` / a pooler
(PgBouncer) — not application code. Median latency stays healthy (35 ms at 50 VUs); the failures
are purely about connection slots.

## Heartbeat throughput — the finding that is not about k6

Measured against a purpose-built 500-tenant × 1000-account (500,000 row) Postgres database, running
the real `handle_refresh_due_accounts` / `process_account` / `run_worker` code:

| | Measured |
|---|---|
| Steady-state demand, 500k accounts on a 6h cycle | **23.15 accounts/sec** |
| Enqueue ceiling (`batch 100` / `tick 60s`) | 1.67 accounts/sec |
| **Drain rate, one worker, real sources** | **0.036 accounts/sec** |

`run_worker` is **strictly serial** — one `dequeue`, one awaited `dispatch`, repeat. Verified
directly: 20 jobs × 1 s sleep drained in 20.24 s, **effective concurrency 0.99**. There is one
worker replica, so the platform has exactly one account-processing slot.

Per-account cost, from **355 real crawls** recorded in `signal_source_runs` on the live stack:
**26.98 s mean** (p50 25.3 s, p95 44.9 s) across 5 sequential sources, plus **0.53 s** for the
scoring agent's LLM rationale (3107 real `agent_runs`) — **~27.7 s per account**.

That gives a hard ceiling of `21600 / 27.7` ≈ **780 accounts** for the whole platform on a 6h
refresh cycle. The live stack has 142, which is why this has never been visible.

Two measured levers before any architecture change:

* **Sources run sequentially** (`for src in self.sources` in `ingestion/service.py`), so an account
  costs the *sum* of its sources. Same 355 crawls, sum vs max: **26.98 s → 14.94 s, a 1.81×**
  speedup from running them concurrently. `process_account` is ~99% await-on-network, so a bounded
  in-flight limit in `run_worker` multiplies throughput almost linearly for near-zero CPU.
* **The claim query does not use an index.** At 500k accounts it seq-scans `accounts` and sorts
  261k rows through a **26 MB external merge on disk** to return 100 — 489 ms warm, 4.58 s cold,
  every tick, growing with the estate. `ix_accounts_last_refreshed_at` cannot serve it because
  `ORDER BY last_refreshed_at ASC NULLS FIRST` is the opposite of a default btree's null ordering.
  Adding `(last_refreshed_at ASC NULLS FIRST)` changed the plan to an index scan that stops at 100:
  **489 ms → 44 ms (11×)**, and makes the cost O(batch) instead of O(estate).

Neither of those closes a 640× gap on its own. Read them as: the pipeline is not tuned, and the
tuning is worth doing before concluding what the architecture must become.

### Reproducing the heartbeat measurement

The 500k-row database is built beside the live one, so nothing touches real data, and the
benchmark forces `NEXUS_QUEUE_BACKEND=memory` so it never enqueues onto live Valkey. The name is
deliberately unmistakable — a scratch database one keystroke away from `nexus` is an accident
waiting for a tired evening:

```bash
docker exec nexus-gtm-postgres-1 psql -U nexus -d postgres -c "CREATE DATABASE nexus_loadtest_scratch OWNER nexus;"
```

Then `alembic upgrade head` against it, seed with `generate_series` (500 tenants × 1000 accounts,
`last_refreshed_at` spread over 12 h so ~half is due), and run `bench_heartbeat.py` with
`NEXUS_DATABASE_URL` pointed at it. Seeding takes ~40 s.

On the dev stack this database already exists at `nexus_loadtest_scratch` (305 MB, migration 0041),
kept so the heartbeat numbers above stay re-checkable rather than having to be believed.

## Interpreting results

k6 prints P50/P90/P95/P99, throughput (`http_reqs`/s), and `http_req_failed`. Watch a soak run next
to the Grafana dashboard: RSS and DB connections should stay flat, and P95 must not drift up.

These API numbers are **hardware-bound and co-location-bound**, not code-bound — the worker was
running real Exa/Firecrawl crawls on the same machine throughout. Re-run against separately
resourced app/worker/Postgres before quoting a production SLO. The heartbeat numbers above are
**not** hardware-bound: the 27 s per account is almost entirely waiting on third-party HTTP, and a
faster machine does not change it.
