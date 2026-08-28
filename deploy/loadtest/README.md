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

`run_worker` **was strictly serial** — one `dequeue`, one awaited `dispatch`, repeat. Verified
directly: 20 jobs × 1 s sleep drained in 20.24 s, **effective concurrency 0.99**. There was one
worker replica, so the platform had exactly one account-processing slot. (Fixed — see "Bounded
in-flight concurrency" below.)

Per-account cost, from **355 real crawls** recorded in `signal_source_runs` on the live stack:
**26.98 s mean** (p50 25.3 s, p95 44.9 s) across 5 sequential sources, plus **0.53 s** for the
scoring agent's LLM rationale (3107 real `agent_runs`) — **~27.7 s per account**.

That gives a hard ceiling of `21600 / 27.7` ≈ **780 accounts** for the whole platform on a 6h
refresh cycle. The live stack has 142, which is why this has never been visible.

### What has been fixed since, and what it measured

Three changes landed (2026-08-05), each re-measured against the same 500k-account database:

* **Tiered refresh** (`nexus/ingestion/tiering.py`). Demand was the assumption nobody had
  questioned: every account on the same 6 h cycle. An account is now HOT (6 h) if the crawl just
  found something, or it has a signal in the last 30 days, or it is in an active cadence, or it is
  on a list — and COLD (72 h) otherwise. Every rule is a reason to stay hot, because wrongly hot
  costs one crawl and wrongly cold means a rep learns about a funding round three days late.

  | share of estate HOT | demand |
  |---|---|
  | 100% (untiered) | 23.15/s |
  | 25% | 7.23/s |
  | 15% | 5.11/s |
  | 10% | 4.05/s |

* **The claim query is now an index scan.** Rather than adding an index to fit the old predicate,
  the due-time is stored (`accounts.next_refresh_at`, migration `0042`) instead of derived from
  `last_refreshed_at IS NULL OR <= cutoff ORDER BY ... ASC NULLS FIRST` — which no btree can serve.
  Measured on the same 500k rows: **489 ms → 5-8 ms warm** (4.58 s → 95 ms cold), and the plan is
  an index scan that stops at the limit instead of a seq scan plus a 26 MB external merge on disk.
  The cost is now O(batch), not O(estate). The driver's remaining ~1.3 s is its 100 sequential
  stamp round-trips, not the query.

* **Sources run concurrently** (`signal_sources_concurrent`, default on, kill switch). Per-account
  crawl **26.98 s → 14.94 s (1.81×)**, from the same 355 crawls' sum-vs-max. Session-bound sources
  are deliberately excluded from the gather: a change detector borrows the caller's TenantSession,
  and SQLAlchemy's AsyncSession is not safe for concurrent use.

**Those three did not close the gap, and it was wrong to suggest they would.** Supply was still
one serial worker: ~15.65 s per account (14.94 crawl + 0.53 scoring + DB) = **0.064
accounts/sec**, against 5.11/s demand at a 15% hot ratio. That is **80×**, down from 640×.

### Bounded in-flight concurrency (2026-08-20) — the fourth change, and its measurement

`run_worker` now runs N consumers over the one queue instead of one. N is **derived from the DB
pool rather than chosen** — `db_pool_size + db_max_overflow - POOL_RESERVE` = 10 + 20 - 5 = **25**
— because every handler runs inside `tasks.tenant_session` and therefore holds a connection for
its whole life. Fanning out wider than the pool buys no throughput; it converts throughput into
`TooManyConnectionsError` for jobs that would otherwise have succeeded. The reserve keeps
connections for the scheduler's advisory lock, the state-metrics sweep and the dead-letter writer,
which needs one at exactly the moment things are already going wrong.
`NEXUS_WORKER_MAX_CONCURRENCY` pins it — set it to 1 to restore the serial loop without a deploy.

Re-measured with the same probes against the same 500k-row scratch database, with `cap=1`
reproducing the old serial loop so this is a like-for-like comparison rather than two numbers from
two builds (`BENCH_CONC_LIMITS=1,default`):

| probe | serial (cap=1) | concurrent (cap=25) | |
|---|---|---|---|
| [D] 100 jobs × 1 s await | 100.67 s — **0.99** effective concurrency | 5.00 s — **19.98** | 20× |
| [D] 50 jobs × 15.65 s await (the real per-account cost) | 782.5 s — 0.064 accounts/sec | **31.38 s — 1.59 accounts/sec** | 24.8× |
| [C] 50 real `process_account`, **sources removed** | 9.98 s — 5.01 accounts/sec | 9.52 s — 5.25 accounts/sec | 1.05× |

**[C] barely moves, and that is the expected answer rather than a disappointment.** [C] deletes
the signal sources, so what remains is the DB + scoring floor: CPU and Postgres round-trips, not
await-on-network. Concurrency overlaps *waiting*, and that probe has nothing left to wait on. It
is still the number worth having, because it says the serialized floor tops out at **5.25
accounts/sec per worker** — comfortably above the 1.59/s the pool cap allows. The network crawl
governs; the database does not. If that floor ever falls below ~1.6/s, the cap stops being the
binding constraint and this table needs re-reading.

So one worker goes **0.064 → 1.59 accounts/sec (24.8×)**, and **4 replicas clear 5.11/s** at a 15%
hot ratio. Four containers is a normal answer; 623 was not.

What the [D] rows are and are not: a sleep stands in for the handler, so they measure the *loop's*
willingness to overlap network waits — the property that governs throughput while
`process_account` is ~99% await-on-network. They are not an end-to-end run against live Exa /
Firecrawl. Read 1.59/s as "the loop can now sustain 25 concurrent crawls", not as a promise about
any particular provider's rate limits.

Durability is unchanged and is tested *under* concurrency in `tests/test_worker_concurrency.py`:
retries, dead-lettering, and the shutdown flush of in-flight backoffs. One ordering constraint is
new and load-bearing — the flush runs **after** the in-flight drain, because flushing first drops
exactly the retries scheduled during shutdown. There is a test that fails if it moves.

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
