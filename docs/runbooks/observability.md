# Runbook — Observability (O-2): Prometheus + Grafana

Metrics, dashboards, and alert rules for the NEXUS GTM stack.

## Bring it up

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

This adds Prometheus + Grafana and sets `NEXUS_METRICS_ENABLED=true` on the app so `/metrics` is
exposed (the endpoint is opt-in — the instrumentator wraps every request, so it is off unless
enabled).

| Surface | URL | Notes |
|---|---|---|
| App metrics | http://localhost:8000/metrics | Prometheus exposition (`http_requests_total`, `http_request_duration_seconds`, …) |
| Prometheus | http://localhost:9090 | Targets → `nexus-app` should be **up**; Alerts → 4 rules loaded |
| Grafana | http://localhost:3000 | `admin` / `admin` (override `GRAFANA_USER`/`GRAFANA_PASSWORD`) |

The Prometheus datasource and the **NEXUS GTM — Service Overview** dashboard are auto-provisioned
(`deploy/monitoring/grafana/provisioning`). The dashboard shows: app up/down, request rate by
status, 5xx ratio, P50/P95/P99 latency, and in-flight requests.

## Alert rules (`deploy/monitoring/alerts.yml`)

| Alert | Fires when | Severity |
|---|---|---|
| `AppDown` | `/metrics` unscrapeable for 1m | critical |
| `HighServerErrorRate` | 5xx ratio > 2% for 5m | critical |
| `HighApiLatencyP95` | P95 (excl. agent endpoints) > 1.5s for 10m | warning |
| `NoTrafficScraped` | zero requests for 15m (up but idle) | warning |

These are the deployment auto-rollback signals from the remediation plan. To actually deliver the
alerts, add an Alertmanager service and point Prometheus at it (`alerting:` block) with your
Slack/PagerDuty receiver — the rules are ready; only the notification sink is environment-specific.

## SLOs (pilot targets)

- Availability 99.5% monthly (error budget ≈ 3.6 h).
- API P95 < 500 ms (excluding the intentionally-slow `/api/agents/*` endpoints); agent P95 < 8 s.
- Exactly one scheduler leader (`sum(scheduler_is_leader) == 1`) once that gauge is exported.

## Production notes

- In production, Prometheus/Grafana usually run on a separate monitoring host or a managed service
  (Grafana Cloud, AMP). This compose overlay is for local/single-VM.
- The cloud Terraform (`deploy/cloud/aws`, `deploy/cloud/azure`) already provisions managed metrics
  + alarms for those targets; this overlay covers the docker-compose path.
