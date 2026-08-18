import { useMemo, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Button, EmptyState, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { cn } from "@/lib/cn";
import type { HealthDependency, HealthRoute, PlatformHealth } from "@/lib/types";
import styles from "./AdminHealthPage.module.css";

/**
 * Platform health console.
 *
 * The design problem here is not "show statuses". It is that **most of this page is not a pass**:
 * of ~200 routes, 123 mutate and are deliberately never called, so they carry no verdict at all.
 * A console that rendered those as anything green-adjacent would be lying, and an operator would
 * trust it during exactly the incident where it matters.
 *
 * So `not_probed` is rendered as a *typographic absence* (an em-rule and grey text), never as a
 * pill, a dot, or a colour. Only the two states we actually verified — `ok` and `error` — get a
 * status marker. The eye can find the verdicts without reading, which is the whole job.
 *
 * `unconfigured` is likewise not a warning: "no Stripe key" and "Stripe rejected our key" send an
 * operator to different places, and collapsing them is how this codebase has repeatedly produced a
 * confident wrong diagnosis.
 */

const STATUS_TONE = {
  ok: "success",
  degraded: "warning",
  unconfigured: "neutral",
  error: "danger",
} as const;

const STATUS_LABEL = {
  ok: "Healthy",
  degraded: "Degraded",
  unconfigured: "Not configured",
  error: "Failing",
} as const;

const OVERALL_COPY = {
  ok: "Everything probed is answering.",
  degraded: "Running, with dependencies that need attention.",
  unconfigured: "Running, with dependencies that were never set up.",
  error: "Something probed is failing right now.",
} as const;

function latency(ms: number | null): string {
  if (ms == null) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

/** Dependencies are the part that was genuinely tested, so they lead and they get room. */
function DependencyRow({ dep }: { dep: HealthDependency }) {
  return (
    <li className={cn(styles.dep, styles[`dep_${dep.status}`])}>
      <span className={styles.depMarker} aria-hidden="true" />
      <div className={styles.depBody}>
        <div className={styles.depTop}>
          <h3 className={styles.depName}>{dep.name}</h3>
          <Badge tone={STATUS_TONE[dep.status]}>{STATUS_LABEL[dep.status]}</Badge>
          {dep.latency_ms != null && (
            <span className={styles.depLatency}>{latency(dep.latency_ms)}</span>
          )}
        </div>
        {/* The detail is the actual finding ("no webhook endpoint registered"), not a subtitle.
            It gets full-width prose measure rather than being truncated into a table cell. */}
        {dep.detail && <p className={styles.depDetail}>{dep.detail}</p>}
      </div>
    </li>
  );
}

type RouteFilter = "all" | "probed" | "not_probed" | "failing";

export function AdminHealthPage() {
  const api = useApiClient();
  const [nonce, setNonce] = useState(0);
  const health = useApi<PlatformHealth>((signal) => api.platformHealth(signal), [nonce]);
  const [filter, setFilter] = useState<RouteFilter>("all");
  const [query, setQuery] = useState("");

  const data = health.data;

  const routes = useMemo(() => {
    const all = data?.routes ?? [];
    const needle = query.trim().toLowerCase();
    return all.filter((r) => {
      if (filter === "probed" && r.status === "not_probed") return false;
      if (filter === "not_probed" && r.status !== "not_probed") return false;
      if (filter === "failing" && r.status !== "error") return false;
      if (needle && !r.path.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [data, filter, query]);

  const columns: Column<HealthRoute>[] = [
    {
      key: "method",
      header: "Method",
      width: "84px",
      render: (r) => <span className={cn(styles.method, styles[`m_${r.method}`])}>{r.method}</span>,
    },
    {
      key: "path",
      header: "Path",
      sortValue: (r) => r.path,
      render: (r) => <span className={styles.path}>{r.path}</span>,
    },
    {
      key: "auth",
      header: "Access",
      width: "150px",
      render: (r) => <span className={styles.auth}>{r.auth}</span>,
    },
    {
      key: "status",
      header: "Verdict",
      width: "230px",
      render: (r) =>
        r.status === "not_probed" ? (
          // Deliberately NOT a badge. An unprobed route has no verdict, and dressing it as one
          // would make 123 untested routes read as passing.
          <span className={styles.unprobed} title={r.reason}>
            <span className={styles.rule} aria-hidden="true">
              —
            </span>
            <span className={styles.unprobedText}>not probed</span>
          </span>
        ) : (
          <span className={styles.verdict}>
            <Badge tone={r.status === "ok" ? "success" : "danger"} dot>
              {r.http_status ?? (r.status === "ok" ? "ok" : "error")}
            </Badge>
            {r.latency_ms != null && (
              <span className={styles.depLatency}>{latency(r.latency_ms)}</span>
            )}
          </span>
        ),
    },
    {
      key: "reason",
      header: "Note",
      render: (r) => <span className={styles.reason}>{r.reason}</span>,
    },
  ];

  return (
    <>
      <PageHeader
        title="Platform health"
        description="Live dependency probes, and the full API surface with what was actually tested."
        actions={
          <Button
            variant="secondary"
            onClick={() => setNonce((n) => n + 1)}
            loading={health.loading}
          >
            Re-run probes
          </Button>
        }
      />

      <DataState
        state={health}
        errorTitle="Could not build a health report"
        skeleton={
          <div className={styles.page}>
            <Skeleton width="100%" height={72} />
            <Skeleton width="100%" height={280} />
            <Skeleton width="100%" height={320} />
          </div>
        }
        isEmpty={(report) => report.dependencies.length === 0 && report.routes.length === 0}
        empty={
          <EmptyState
            title="No health report"
            description="The console built an empty report. Re-run the probes, or check the server logs."
          />
        }
      >
        {(report: PlatformHealth) => (
          <div className={styles.page}>
            <section
              className={cn(styles.verdictBar, styles[`bar_${report.overall}`])}
              aria-live="polite"
            >
              <div>
                <p className={styles.verdictLabel}>{STATUS_LABEL[report.overall]}</p>
                <p className={styles.verdictCopy}>{OVERALL_COPY[report.overall]}</p>
              </div>
              <p className={styles.stamp}>
                Probed {new Date(report.generated_at).toLocaleTimeString()}
              </p>
            </section>

            <section className={styles.section} aria-labelledby="deps-heading">
              <div className={styles.sectionHead}>
                <h2 id="deps-heading" className={styles.sectionTitle}>
                  Dependencies
                </h2>
                <p className={styles.sectionNote}>
                  Every one of these was called just now. {report.summary.dependencies_ok} of{" "}
                  {report.summary.dependencies_total} answered cleanly.
                </p>
              </div>
              <ul className={styles.depList}>
                {report.dependencies.map((dep) => (
                  <DependencyRow key={dep.name} dep={dep} />
                ))}
              </ul>
            </section>

            <section className={styles.section} aria-labelledby="routes-heading">
              <div className={styles.sectionHead}>
                <h2 id="routes-heading" className={styles.sectionTitle}>
                  API surface
                </h2>
                <p className={styles.sectionNote}>
                  {report.summary.routes_total} routes. {report.summary.routes_probed} were called;{" "}
                  {report.summary.routes_not_probed} were not, because calling them would change
                  data. Those carry no verdict rather than an assumed one.
                </p>
              </div>

              <div className={styles.controls}>
                <div className={styles.filters} role="group" aria-label="Filter routes">
                  {(
                    [
                      ["all", `All ${report.summary.routes_total}`],
                      ["probed", `Probed ${report.summary.routes_probed}`],
                      ["not_probed", `Not probed ${report.summary.routes_not_probed}`],
                      ["failing", `Failing ${report.summary.routes_failing}`],
                    ] as [RouteFilter, string][]
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      className={cn(styles.filter, filter === value && styles.filterOn)}
                      aria-pressed={filter === value}
                      onClick={() => setFilter(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <label className={styles.search}>
                  <span className="sr-only">Filter by path</span>
                  <input
                    type="search"
                    value={query}
                    placeholder="Filter by path"
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </label>
              </div>

              {routes.length === 0 ? (
                <EmptyState
                  title="No routes match"
                  description="Clear the filter or search for a different path."
                />
              ) : (
                <DataTable
                  columns={columns}
                  rows={routes}
                  getRowKey={(r) => `${r.method} ${r.path}`}
                  caption="Registered API routes and what was actually probed"
                />
              )}
            </section>
          </div>
        )}
      </DataState>
    </>
  );
}

export default AdminHealthPage;
