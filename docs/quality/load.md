# Load testing — k6 + Gatling on staging

The load-testing framework for NEXUS GTM. One scenario catalogue, two tools with distinct jobs,
run against **staging** under hard caps, outside Indian working hours, with synthetic data.

- **Catalogue:** [`quality/load/scenarios.yaml`](../../quality/load/scenarios.yaml) — journeys, weights,
  think times, endpoints, personas, paid journeys, load profiles.
- **Environments and caps:** [`quality/load/environments.yaml`](../../quality/load/environments.yaml).
- **Runner:** [`scripts/load/run.py`](../../scripts/load/run.py).
- **Capacity model:** [load-capacity.md](load-capacity.md).
- **Results:** [`docs/quality/load-results/`](load-results/), with the trend table [below](#trend).

## Who does what

| | k6 | Gatling |
|---|---|---|
| Job | Per-release **regression gate** | **Exploration**: where and how it breaks |
| Profiles | `smoke`, `load`, `soak` | `stress`, `spike`, `breakpoint`, `capacity` |
| Verdict | Fails the run (exit 1) on an SLO breach | Records the SLO verdict as a finding; exit 0 |
| Length | Minutes (soak: 30 min default) | 8–22 min |
| Output | Summary JSON, Prometheus remote-write to Grafana Cloud | HTML report (artifact) + summary JSON |
| Code | [`deploy/loadtest/k6/`](../../deploy/loadtest/k6/) | [`deploy/loadtest/gatling/`](../../deploy/loadtest/gatling/) |

The profile decides the tool. `run.py --tool k6 --profile stress` is refused, so the division of
labour cannot erode one convenient exception at a time.

### Why Gatling's JavaScript/TypeScript SDK, not the Java DSL

The team writes Python and TypeScript. The TypeScript SDK is the choice for four reasons:

- **The catalogue is shared, not translated.** Simulations `import` the generated
  `catalogue.json` (esbuild bundles it), exactly as the k6 scripts `open()` it. With the Java DSL
  that JSON needs a Jackson model, and the model is a second definition of the schema that drifts.
- **Same engine, same reports.** The JS SDK drives the same Gatling runtime. Injection profiles,
  assertions, `resources()` for parallel page requests, the global store and the HTML report are
  the Java DSL's.
- **No JVM build to own.** No Maven or Gradle wrapper, plugin versions or `pom.xml`. `npm ci` plus a
  pinned `@gatling.io/cli`, type-checked with `tsc` like the frontend.
- **Reviewable by the people who will change it.** A rep journey is a YAML edit. The interpreter
  is ~300 lines of TypeScript the frontend team already reads.

Costs, accepted knowingly:

- The SDK is younger, so there are fewer examples.
- The CLI needs Node ≥ 24. It runs in `node:24-bookworm`, so CI's Node 20 does not matter.
- The first run downloads a ~400 MB runtime bundle, cached in the `nexus-loadtest-gatling-home`
  Docker volume.
- `npx gatling` turns Gatling's "assertions failed" exit code 2 into a generic 1, so the runner
  reads the assertion lines from the log instead of trusting the exit code.
- Java libraries cannot be imported. Nothing here needs them.

## Layout

```
quality/load/
  scenarios.yaml          the catalogue (edit this)
  environments.yaml       targets, hard caps, run windows, guards, forbidden production hosts
  slo.fixture.yaml        stand-in for quality/slo.yaml until the Quality foundation merges
  generated/              catalogue.json + slo.json — generated, committed, drift-checked
deploy/loadtest/
  k6/{smoke,load,soak}.js         entry points (name a profile, nothing else)
  k6/lib/                         config, auth (token pool + staggered re-login), journey interpreter, runtime
  gatling/src/*.gatling.ts        stress, spike, breakpoint, capacity entry points
  gatling/src/lib/                config, journey interpreter (+ in-simulation guard), simulation builder
  bench_heartbeat.py              worker-throughput benchmark (see "Worker and heartbeat")
scripts/load/
  run.py                  the runner       catalogue.py  validate / generate / check / plan
  safety.py               caps, windows    preflight.py  health + persona logins
  testdata.py             synth/existing/anon            probes.py  canary + worker probe
  tools.py                Docker launch + result parsers results.py  result files + trend table
  capacity.py             connection budget model        tests/     offline unit tests
azure-pipelines-load-k6.yml, azure-pipelines-load-gatling.yml   manual, disabled by default
```

## The scenario catalogue

Every request path was read off `frontend/src/lib/api.ts` and the page that calls it. A journey
replays what the SPA sends when a page loads, including the requests a page fires together:
Account detail fires three at once, the Dashboard four.

| Journey | Persona | Weight | Steps (page → requests) | Think time |
|---|---|---|---|---|
| `rep_daily` | rep | 55 | login → app shell (`/billing/entitlements`) → Inbox → Accounts (picks one) → Account detail (account, its contacts, its signals) → Signals → Contacts | 2–6 s |
| `rep_landing` | rep | 20 | login → app shell → Dashboard (`/analytics/overview`, inbox, open alerts, 6 signals) | 2–6 s |
| `manager_review` | manager | 15 | login → app shell → analytics overview + activity → Lists | 3–8 s |
| `admin_settings` | admin | 8 | login → app shell → Members → Billing (usage, credits, invoices, plans) → Settings/Configuration (automation, signal preferences, email accounts, CRM sync status) | 3–10 s |
| `platform_customers` | platform_admin | 2 | login → whoami → customer directory → one customer's usage | 5–15 s |

**Default journeys are read-only.** `catalogue.py` rejects any non-GET request in `journeys`, so a
default run cannot call Groq, Exa, Firecrawl, Apify, Reacher or Stripe. The one exception, the
login POST, is handled by the token manager and not by the journey.

**Paid journeys** (`paid_journeys`) are off unless named with `--budget <journey>=<calls>`. Each is
one virtual user making exactly that many paid calls, capped by a documented maximum. The runner
asks for confirmation, or `--yes`.

| Paid journey | Spends on | Max calls |
|---|---|---|
| `research_brief` | Groq (+ web search) | 3 |
| `email_draft` | Groq | 3 |
| `lookalikes` | Exa | 2 |
| `find_contacts` | Exa, Apify, Reacher | 1 |
| `verify_contact` | Reacher | 3 |
| `signal_ingest` | Firecrawl, Brave/Serper, Groq | 1 |

Stripe is a `forbidden_provider`: no journey may declare it, budget or not.

### How k6 and Gatling are kept from diverging

Neither tool holds a journey. `python scripts/load/catalogue.py generate` resolves the YAML into
`quality/load/generated/catalogue.json`. `deploy/loadtest/k6/lib/journey.js` and
`deploy/loadtest/gatling/src/lib/journey.ts` are *interpreters* of that JSON.
`python scripts/load/catalogue.py check` fails when:

1. the generated JSON is stale against the YAML;
2. a journey uses a step feature (parallel requests, extraction, query variables, JSON bodies,
   paid requests…) that either interpreter does not list in its `SUPPORTED_FEATURES`;
3. a profile uses a load shape that its tool's interpreter does not list in `SUPPORTED_KINDS`, or has
   no entry point;
4. a journey id appears as a string literal in either tool's code, i.e. a scenario written by hand.

The runner runs `check` before every run and refuses on drift. The load pipelines run it as their
first step.

## Profiles

Defaults, as resolved by `python scripts/load/catalogue.py plan --profile <p> --env staging`.
Every default fits the staging caps; `catalogue.py` refuses a default that does not.

| Profile | Tool | Shape | Peak | Duration (incl. grace) | Est. peak req/s |
|---|---|---|---|---|---|
| `smoke` | k6 | 1 iteration per journey, no think time | 5 VUs | ≤ 4 min | ~29 |
| `load` | k6 | ramp 10 → 20 VUs, hold 3 min | 20 VUs | 7 min | ~10 |
| `soak` | k6 | 10 VUs constant | 10 VUs | 31.5 min | ~5 |
| `stress` | Gatling | open: 0.5 → 1.0 users/s | 1.0 users/s (≈17 concurrent) | 10 min | ~7 |
| `spike` | Gatling | open: 0.2 → 2.0 users/s in 10 s, hold 60 s, recover | 2.0 users/s (≈34) | 8 min | ~14 |
| `breakpoint` | Gatling | open stairs 0.25 → 2.0 users/s, 90 s levels | 2.0 users/s (≈34) | 15 min | ~14 |
| `capacity` | Gatling | closed stairs 5 → 30 concurrent, 3 min levels | 30 concurrent | 22 min | ~15 |

Overrides, all still bounded by the caps:

- `--vus` (load, soak), `--duration-s` (soak), `--iterations` (smoke);
- `--peak-rate` (stress, spike), `--levels` (breakpoint, capacity);
- `--duration-scale 0.01..1` shortens every stage for a rehearsal;
- `--exclude-journey`.

## Running

Prerequisites: Docker, and Python ≥ 3.11 with PyYAML (`pip install pyyaml`). Nothing else is
installed locally; k6 (`grafana/k6:2.2.0`) and Gatling (`node:24-bookworm` + `@gatling.io/cli
3.15.105`) run in containers.

```bash
python scripts/load/run.py --profile smoke --env local --data existing --exclude-journey platform_customers
```

```bash
python scripts/load/run.py --profile load --env staging --data synth --dry-run
```

```bash
python scripts/load/run.py --profile breakpoint --env staging --data existing --duration-scale 0.5 --yes
```

`--dry-run` does everything except load:

- drift check, plan, caps, window;
- pre-flight health checks;
- test data and persona logins, then cleanup.

Personas come from the environment:

- `LT_REP_EMAIL` / `LT_REP_PASSWORD`, and likewise `LT_MANAGER_*`, `LT_ADMIN_*`,
  `LT_PLATFORM_ADMIN_*`;
- or `LT_PERSONAS_FILE` pointing at a JSON file **outside** the repository;
- in ADO, the `gtm-loadtest` variable group.

Personas must not be MFA-enrolled, and each must hold the role its journey expects. The runner
checks both before any load.

**Where it runs today:**

- **A machine with Docker** — a laptop or a VM.
- **The ADO pipelines** — Microsoft-hosted `ubuntu-latest` has Docker; free-tier jobs stop at
  60 min, which fits every default profile.
- **Azure Cloud Shell has no Docker daemon.** From Cloud Shell, run the k6 scripts directly with a
  k6 binary:

  ```bash
  LT_REPO=$PWD LT_ENV=staging LT_REP_EMAIL=... LT_REP_PASSWORD=... k6 run deploy/loadtest/k6/load.js
  ```

  Such a run still enforces the caps, allowlist and thresholds inside the script. It skips the
  runner's window, notice, canary and data steps, and Cloud Shell's 20-minute idle timeout ends
  long sessions.

Direct tool runs (no runner) read the catalogue's default plan and the committed SLO export:

```bash
docker run --rm --network nexus-gtm_default -v "$PWD:/repo:ro" -e LT_ENV=local -e LT_REP_EMAIL -e LT_REP_PASSWORD -e LT_MANAGER_EMAIL -e LT_MANAGER_PASSWORD -e LT_ADMIN_EMAIL -e LT_ADMIN_PASSWORD -e LT_PLATFORM_ADMIN_EMAIL -e LT_PLATFORM_ADMIN_PASSWORD grafana/k6:2.2.0 run /repo/deploy/loadtest/k6/smoke.js
```

```bash
cd deploy/loadtest/gatling && npm ci && npx gatling run --typescript --simulation stress
```

Exit codes:

- `0` — ok;
- `1` — SLO breach on a gating (k6) profile;
- `2` — refused before any load (caps, window, drift, pre-flight, data, personas);
- `3` — aborted by a guard;
- `4` — the tool itself failed.

## Staging safety

- **Hard caps** (`environments.yaml`, staging): 60 VUs, 60 min, 60 req/s, 5 new users/s.
  - The runner refuses a plan whose envelope exceeds any of them.
  - k6's `rps` option enforces the request-rate ceiling.
  - Both tools re-check the caps at start-up, so a hand-run script is capped too.
  - No CLI flag raises a cap; that is a reviewed edit to `environments.yaml`.
- **Target:**
  - `forbidden_hosts` names production (`gtm.infojoy.com`, `gtm-prod-app*`). It is checked first,
    as a prefix, by the runner and by both tools.
  - Every other host must be in the environment's `allowed_hosts`.
- **Run window:**
  - Staging blocks Monday–Saturday 08:00–21:00 IST; Sunday is open all day.
  - The whole run, plus tool setup time, must fit outside the blocked span.
  - A refusal names the next start that fits.
  - `--ignore-window --reason "..."` exists; the reason is recorded in the result and announced in
    the notice.
- **Pre-flight:** `/health` must be ok, `/ready` must report `db=up`, and 5 readiness probes form
  a latency baseline. `/metrics` is informational.
- **Guards** stop a run to protect staging; SLOs only grade it. Three independent layers:
  1. **In the tool.** k6 has `abortOnFail` thresholds. Gatling has a rolling 60 s window in the
     global store with `stopLoadGeneratorIf`. Both trip on error rate ≥ 5%, or p95 > 3 s, which
     is exactly when > 5% of requests are slower than 3 s. Gatling also trips above 1.2 × the RPS
     cap.
  2. **The runner's canary.** One `GET /ready` every 10 s from outside the tool. The container is
     stopped if half of the last 6 fail, or if their median exceeds 3 s.
  3. **A hard timeout.** Planned duration + 3 min (k6), or + 10 min for Gatling setup; then
     `docker stop` (via `--init`, so the signal reaches the tool).
- **Notice.** "Staging is under load" when a run starts and ends:
  - stdout always;
  - an ADO warning annotation in pipelines;
  - `LT_NOTICE_WEBHOOK` (a Slack/Teams incoming webhook);
  - a Grafana annotation (`LT_GRAFANA_URL` + `LT_GRAFANA_TOKEN`) tagged `load-test`.
- **Tagging.** Every request carries `X-Load-Test: <run-id>` and
  `User-Agent: nexus-loadtest/<tool> <run-id>`.
- **Confirmation.** Staging and paid-provider runs need an interactive "yes" or `--yes`.

## Test data

| `--data` | What | Cleanup |
|---|---|---|
| `synth` (default) | `scripts/testdata/synth.py` creates `qa-synth-lt-<run>` tenants and `@qa.example.com` personas before the run | Always, in a `finally`, even after an abort |
| `existing` | Personas supplied by the environment; their tenants' data as it is | None; the result says so |
| `anon` (opt-in) | Anonymised production copy restored into staging (`qa-anon-*`); needs `LT_ANON_CONFIRM=yes` + `LT_ANON_TENANT` | None |

> **Not merged yet:** the Quality foundation's `scripts/testdata/synth.py`. Until it lands,
> `--data synth` refuses with that message. Use `--data existing`.
>
> The adapter contract lives in `scripts/load/testdata.py`:
>
> - create: `--prefix P --tenants N --accounts M --json`, printing
>   `{tenants, personas:[{role,email,password,tenant}]}`;
> - clean up: `--cleanup --prefix P --json`.
>
> Staging Postgres has no public access, so on staging the generator runs inside the VNet. Set
> `LT_SYNTH_COMMAND` to wrap it (`az containerapp exec ...`).
>
> The adapter refuses any tenant outside the prefix and any persona outside `@qa.example.com`.

## SLOs

Thresholds come from the Quality foundation's `quality/slo.yaml`, via its JSON export.

> **Fixture in use:** that file has not merged, so `scripts/load/slo.py` reads
> [`quality/load/slo.fixture.yaml`](../../quality/load/slo.fixture.yaml). It has the same schema and
> the owner's `baseline`/`strict` values. Every export and result file carries
> `"is_fixture": true` while that is so, and the trend table marks it.
>
> Once the real file exists it is used with no code change. Point `LT_SLO_EXPORT` at the
> foundation's exported JSON to use it verbatim.

| SLO field | k6 | Gatling |
|---|---|---|
| `api.p95_ms`, `api.p99_ms` | `http_req_duration{kind:api}` p(95), p(99) | `global().responseTime().percentile(95/99)` |
| `api.error_rate_pct` | `http_req_failed{kind:api}` rate | `global().failedRequests().percent()` |
| per journey | `http_req_duration{journey:<id>}` p(95) over the journey's requests | `details(<journey>, <request>)` p95 for each request type (a group's own time is cumulative, so it cannot be graded against a request SLO) |
| `routes."<METHOD> <path>"` | `http_req_duration{name:<METHOD> <path with :vars>}` | `details(<journey>, <request>)` wherever that route appears |

The runner also grades every run itself from the normalised metrics (`results.slo_verdict`), so
both tools are judged by one rule. `--slo-profile strict` previews the stricter profile without
editing the SLO file.

## Results and trend

Every recorded run writes `docs/quality/load-results/<date>-<tool>-<profile>.json`, schema
`nexus.load.result/v1`. It holds:

- run id, git commit, environment, tool image;
- the resolved plan and envelope, caps, guards, window verdict (and any override reason);
- the SLO source (fixture or real), data mode and cleanup, pre-flight;
- normalised metrics — requests, req/s, p50/p95/p99, error rate, `by_journey`, `by_request`;
- the SLO verdict, the guard outcome, and worker-probe samples.

Staging runs are recorded by default; local runs keep only `deploy/loadtest/.runs/<run-id>/`
(gitignored), unless `--record` is given. The table below is regenerated from the result files
after each recorded run. Do not edit between the markers.

<a id="trend"></a>
<!-- load-trend:begin (generated by scripts/load/run.py; do not edit) -->
<!-- load-trend:end -->

## Monitoring (Grafana Cloud)

The Operations task owns the Grafana Cloud stack. This framework feeds it:

- **k6 → Prometheus remote-write.**
  - Set `LT_PROM_RW_URL`, `LT_PROM_RW_USER` and `LT_PROM_RW_TOKEN`; the runner adds
    `-o experimental-prometheus-rw`.
  - Trend stats are exported as `p(50),p(95),p(99),max`, pushed every 30 s. The free tier keeps
    1-minute resolution anyway.
  - Metric names: `k6_http_req_duration_p50|p95|p99|max`, `k6_http_reqs_total`,
    `k6_http_req_failed_rate`, `k6_checks_rate`, `k6_vus`, `k6_vus_max`, `k6_iterations_total`.
  - Framework metrics: `k6_lt_journey_duration_p95`, `k6_lt_logins_total{persona,outcome}`,
    `k6_lt_skipped_steps_total{journey,step}`, `k6_lt_reauth_retries_total`,
    `k6_lt_paid_calls_total`.
  - Labels: `testid` (the run id), `env`, `profile`, `scenario`, `journey`, `step`, `persona`,
    `kind` (`api`/`login`/`paid`), `name` (route template), `method`, `status`,
    `expected_response`.
  - `url`, `vu` and `iter` are dropped (`systemTags`), keeping cardinality bounded.
- **Gatling → artifacts.** The HTML report directory and the result JSON are published by the
  Gatling pipeline (`gatling-report`, `load-result`).
- **App and platform series to chart beside a run** (the dashboard plan, built jointly):

| Panel | Series |
|---|---|
| Load applied | `k6_vus`, `k6_http_reqs_total` rate by `journey` — filter `testid` |
| Latency seen by users | `k6_http_req_duration_p95` by `name`; app `http_request_duration_seconds_bucket{handler}` (prometheus-fastapi-instrumentator) |
| Errors | `k6_http_req_failed_rate`; app `http_requests_total{status=~"5.."}` by `handler` |
| **Postgres connections** (the binding constraint) | Azure Monitor `active_connections` on the Flexible Server vs 35 |
| Postgres CPU and **CPU credits** (B1ms is burstable) | `cpu_percent`, `cpu_credits_remaining` |
| Replicas | Container Apps `Replicas` for `gtm-staging-app` (scale rule: 50 concurrent requests) |
| Worker / heartbeat | `nexus_queue_depth`, `nexus_dead_letter_jobs`, `rate(nexus_jobs_total[5m])` |
| Run markers | Grafana annotations tagged `load-test` |

## Worker and heartbeat

HTTP load cannot measure the worker: it has no ingress and does its work off-request. Two
separate instruments cover it, and neither puts k6 load on it.

- **Worker probe, during every run.** `scripts/load/probes.py` samples `nexus_queue_depth`,
  `nexus_dead_letter_jobs` and `rate(nexus_jobs_total[5m])` every 60 s:
  - locally, from the compose Prometheus (`127.0.0.1:9090`);
  - on staging, from Grafana Cloud (`LT_GRAFANA_PROM_URL` / `_USER` / `_TOKEN`) once the Operations
    task ships worker metrics there.

  Until then staging records `"status": "unmeasured"` with the reason, never 0: a missing gauge
  must not read as a healthy queue. The question it answers is whether API load backs up the
  queue (e.g. inbox tasks or plays enqueued by reads) and whether dead letters appear. Compare
  depth with the SLO's `queue_lag_minutes`.
- **Throughput benchmark, separately.** [`deploy/loadtest/bench_heartbeat.py`](../../deploy/loadtest/bench_heartbeat.py)
  runs the real `handle_refresh_due_accounts` / `process_account` / `run_worker` against a
  scratch database (500 tenants × 1,000 accounts). It answers a different question: can the
  refresh pipeline drain demand (0.064 → 1.59 accounts/s per worker, 2026-08-20). Its numbers
  are not hardware-bound (the crawl is waiting on third parties), so they are not re-measured on
  staging. Re-run it when `run_worker`, the pool size or the tiering rules change. Method and
  history are in [`deploy/loadtest/README.md`](../../deploy/loadtest/README.md).

## CI

- `azure-pipelines-load-k6.yml` and `azure-pipelines-load-gatling.yml` are **manual only**:
  `trigger: none`, `pr: none`, no schedule.
- Create each in ADO, then set it to **Disabled** until it is needed.
- Each requires the `confirmStaging` parameter and reads persona credentials from the
  `gtm-loadtest` variable group (secrets).
- Both stay on the free Microsoft-hosted pool until the self-hosted agent VM is proven by its own
  task. Per-push CI is untouched.
- The offline unit tests are `python -m pytest scripts/load/tests -q`. They are not part of
  per-push CI, whose scope is `nexus` and `tests`.
