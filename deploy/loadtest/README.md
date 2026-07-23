# Load & Soak testing (O-3)

k6 scripts that establish a performance baseline and catch regressions before they reach users.

## Run

Against the local docker stack (app on :8000). k6 runs in a container — no local install needed:

```bash
# Load: ramp 0 → 200 VUs, ~2 min. Fails the run if SLO thresholds break.
docker run --rm -e BASE_URL=http://host.docker.internal:8000 \
  -v "$PWD/deploy/loadtest:/scripts" grafana/k6 run /scripts/load.js

# Soak: steady VUs held for a long duration to surface leaks / drift.
docker run --rm -e BASE_URL=http://host.docker.internal:8000 \
  -e SOAK_VUS=30 -e SOAK_DURATION=30m \
  -v "$PWD/deploy/loadtest:/scripts" grafana/k6 run /scripts/soak.js
```

(On Windows PowerShell use `-v "${PWD}\deploy\loadtest:/scripts"`.) Point `BASE_URL` at staging to
baseline production-shaped hardware.

Each script signs up a fresh workspace in `setup()`, then exercises the rep's read-hot path
(`/api/accounts`, `/api/signals`, `/api/inbox`, `/api/analytics/overview`). Thresholds encode the
SLOs: reads P95 < 500 ms, P99 < 1.5 s, error rate < 1%.

## Interpreting results

k6 prints P50/P90/P95/P99, throughput (`http_reqs`/s), and `http_req_failed`. Watch the soak run
next to the Grafana dashboard: RSS and DB connections should stay flat, and P95 must not drift up.

## Local baseline (single laptop, Docker Desktop) — reference only

Measured on one developer laptop with the **app, worker, Postgres, Prometheus and Grafana all
co-located** under the Docker Desktop VM:

| Scenario | Failures | Median | P95 | Notes |
|---|---|---|---|---|
| 20 VUs, 45s | **0.00%** | **103 ms** | 7.4 s | App request latency is excellent (median 103 ms); the tail is the co-located worker's real Exa/Groq discovery + the Docker Desktop VM tax stealing cores. |
| 200 VUs, 2m | 0.34% | 2.18 s | 14 s | Stress: the app degrades gracefully (0.34% errors) rather than falling over; the laptop is CPU-saturated. |

**These are hardware-bound, not code-bound.** The median (~100 ms) is the real per-request cost;
the tail reflects a single machine running everything at once. Re-run against a properly sized
target (separate app/worker resources, real multi-core, no Docker Desktop VM) before quoting a
production SLO — that is the environment the 500 ms P95 threshold is written for.
