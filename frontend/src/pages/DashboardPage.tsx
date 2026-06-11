import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { StatCard } from "@/components/StatCard";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Icons,
  Skeleton,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { ActivationChecklist } from "@/components/Onboarding";
import { LiveIndicator } from "@/components/LiveIndicator";
import { useApi } from "@/hooks/useApi";
import type { AsyncState } from "@/hooks/useApi";
import { useLivePoll } from "@/hooks/useLivePoll";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber, formatPercent, humanize, timeAgo } from "@/lib/format";
import { activityTone, priorityTone, severityTone, strengthMeta } from "@/lib/display";
import type {
  ActivityItem,
  AnalyticsOverview,
  Alert,
  InboxTask,
  OutcomeStage,
  OutcomeSummary,
  Role,
  SignalEvent,
} from "@/lib/types";
import styles from "./DashboardPage.module.css";

/** How often the dashboard repolls while the tab is visible (paused when hidden). */
const LIVE_INTERVAL_MS = 12_000;

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

/** The funnel managers read top-down: outreach → reply → meeting → won, plus lost. */
const FUNNEL: { key: OutcomeStage; label: string }[] = [
  { key: "sent", label: "Sent" },
  { key: "replied", label: "Replied" },
  { key: "meeting", label: "Meeting" },
  { key: "won", label: "Won" },
  { key: "lost", label: "Lost" },
];

/** Pick a representative icon for a metric key. */
function statIcon(key: string) {
  const k = key.toLowerCase();
  if (k.includes("account")) return <Icons.BuildingIcon />;
  if (k.includes("signal")) return <Icons.SignalIcon />;
  if (k.includes("alert")) return <Icons.BellIcon />;
  if (k.includes("task") || k.includes("inbox")) return <Icons.InboxIcon />;
  if (k.includes("member") || k.includes("user")) return <Icons.UsersIcon />;
  if (k.includes("contact")) return <Icons.UsersIcon />;
  return <Icons.TrendUpIcon />;
}

const SAMPLE_ACCOUNT = {
  name: "Northwind Logistics",
  domain: "northwind.example",
  industry: "Supply Chain",
  employee_count: 540,
  country: "United States",
  tech_stack: ["Snowflake", "Segment", "Salesforce"],
};

export function DashboardPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const toast = useToast();
  const { session } = useAuth();
  const canViewAttribution = session ? ROLE_RANK[session.role] >= ROLE_RANK.manager : false;
  const [seeding, setSeeding] = useState(false);

  const overview = useApi<AnalyticsOverview>((signal) => api.analyticsOverview(signal), []);
  const inbox = useApi<InboxTask[]>((signal) => api.listInbox(signal), []);
  const alerts = useApi<Alert[]>((signal) => api.listAlerts("open", signal), []);
  const signals = useApi<SignalEvent[]>((signal) => api.listSignals({ limit: 6 }, signal), []);
  // The cross-entity activity feed is a manager analytics surface (view_analytics). Reps skip
  // the request entirely so their dashboard never shows a permission error where the feed sits.
  const activity = useApi<ActivityItem[]>(
    (signal) => (canViewAttribution ? api.analyticsActivity(24, signal) : Promise.resolve([])),
    [canViewAttribution],
  );

  const refreshAll = useCallback(() => {
    overview.refetch();
    inbox.refetch();
    alerts.refetch();
    signals.refetch();
    activity.refetch();
  }, [overview, inbox, alerts, signals, activity]);

  // The "live" pulse: repoll everything visible on an interval, pausing on a hidden tab.
  const { live, lastTick } = useLivePoll(refreshAll, { intervalMs: LIVE_INTERVAL_MS });

  const seedDemo = useCallback(async () => {
    setSeeding(true);
    try {
      const account = await api.createAccount(SAMPLE_ACCOUNT);
      await api.runPipeline(account.id);
      toast.success("Demo account seeded", `${account.name} enriched and scored.`);
      refreshAll();
    } catch (err) {
      toast.error(
        "Couldn't seed demo data",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSeeding(false);
    }
  }, [api, toast, refreshAll]);

  return (
    <div>
      <PageHeader
        title="Dashboard"
        description="Your go-to-market intelligence at a glance."
        actions={
          <>
            <LiveIndicator live={live} lastTick={lastTick} className={styles.live} />
            <Button
              variant="secondary"
              iconLeft={<Icons.RefreshIcon />}
              onClick={refreshAll}
            >
              Refresh
            </Button>
            <Button
              iconLeft={<Icons.SparklesIcon />}
              loading={seeding}
              onClick={seedDemo}
            >
              Seed demo account
            </Button>
          </>
        }
      />

      <ActivationChecklist overview={overview.data} onSeed={seedDemo} seeding={seeding} />

      <DataState
        state={overview}
        skeleton={
          <div className={styles.statSkeleton}>
            {Array.from({ length: 4 }).map((_, i) => (
              <Card key={i} padding="md">
                <Skeleton width="50%" height={12} />
                <div style={{ height: 12 }} />
                <Skeleton width="40%" height={26} />
              </Card>
            ))}
          </div>
        }
        isEmpty={(data) => Object.keys(data).length === 0}
        empty={
          <EmptyState
            icon={<Icons.TrendUpIcon />}
            title="No analytics yet"
            description="Seed a demo account to populate your workspace metrics."
            action={
              <Button iconLeft={<Icons.SparklesIcon />} loading={seeding} onClick={seedDemo}>
                Seed demo account
              </Button>
            }
          />
        }
      >
        {(data) => (
          <div className={styles.stats}>
            {Object.entries(data)
              .slice(0, 8)
              .map(([key, value]) => (
                <StatCard
                  key={key}
                  label={humanize(key)}
                  value={formatNumber(value)}
                  icon={statIcon(key)}
                />
              ))}
          </div>
        )}
      </DataState>

      {canViewAttribution && <OutcomeAttribution onTune={() => navigate("/relevance")} />}

      <div className={styles.columns}>
        <div className={styles.col}>
          {canViewAttribution && <ActivityFeed state={activity} live={live} />}

          <Card padding="md">
            <CardHeader
              title="Top inbox tasks"
              subtitle="Prioritized actions for your accounts"
              actions={
                <Button variant="ghost" size="sm" onClick={() => navigate("/inbox")}>
                  View all
                </Button>
              }
            />
            <DataState
              state={inbox}
              skeleton={<RowsSkeleton />}
              isEmpty={(rows) => rows.length === 0}
              empty={
                <EmptyState
                  compact
                  icon={<Icons.InboxIcon />}
                  title="Inbox zero"
                  description="No pending tasks. New work appears here automatically."
                />
              }
            >
              {(rows) => (
                <div className={styles.list}>
                  {rows.slice(0, 5).map((task) => (
                    <div key={task.id} className={styles.item}>
                      <Badge
                        className={styles.itemAccent}
                        tone={priorityTone(task.priority)}
                        dot
                      >
                        P{task.priority}
                      </Badge>
                      <div className={styles.itemBody}>
                        <div className={styles.itemTitle}>{task.title}</div>
                        <div className={styles.itemMeta}>{task.reason}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </DataState>
          </Card>
        </div>

        <div className={styles.col}>
          <Card padding="md">
            <CardHeader
              title="Open alerts"
              actions={
                <Button variant="ghost" size="sm" onClick={() => navigate("/alerts")}>
                  View all
                </Button>
              }
            />
            <DataState
              state={alerts}
              skeleton={<RowsSkeleton rows={3} />}
              isEmpty={(rows) => rows.length === 0}
              empty={
                <EmptyState
                  compact
                  icon={<Icons.BellIcon />}
                  title="No open alerts"
                  description="You're all caught up."
                />
              }
            >
              {(rows) => (
                <div className={styles.list}>
                  {rows.slice(0, 4).map((alert) => (
                    <div key={alert.id} className={styles.item}>
                      <Badge
                        className={styles.itemAccent}
                        tone={severityTone(alert.severity)}
                        dot
                      >
                        {alert.severity}
                      </Badge>
                      <div className={styles.itemBody}>
                        <div className={styles.itemTitle}>{alert.title}</div>
                        <div className={styles.itemMeta}>{alert.body}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </DataState>
          </Card>

          <Card padding="md">
            <CardHeader
              title="Recent signals"
              actions={
                <Button variant="ghost" size="sm" onClick={() => navigate("/signals")}>
                  View all
                </Button>
              }
            />
            <DataState
              state={signals}
              skeleton={<RowsSkeleton rows={3} />}
              isEmpty={(rows) => rows.length === 0}
              empty={
                <EmptyState
                  compact
                  icon={<Icons.SignalIcon />}
                  title="No signals yet"
                  description="Buying signals will surface here as they're detected."
                />
              }
            >
              {(rows) => (
                <div className={styles.list}>
                  {rows.slice(0, 5).map((sig) => {
                    const meta = strengthMeta(sig.strength);
                    return (
                      <div key={sig.id} className={styles.item}>
                        <Badge className={styles.itemAccent} tone={meta.tone} dot>
                          {meta.label}
                        </Badge>
                        <div className={styles.itemBody}>
                          <div className={styles.itemTitle}>{sig.title}</div>
                          <div className={styles.itemMeta}>
                            <span>{humanize(sig.kind)}</span>
                            <span>·</span>
                            <span>{timeAgo(sig.occurred_at)}</span>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </DataState>
          </Card>
        </div>
      </div>
    </div>
  );
}

const ACTIVITY_LABELS: Record<string, string> = {
  signal: "Signal",
  alert: "Alert",
  account_scored: "Scored",
  agent_run: "Agent",
};

/**
 * The live pulse: a merged, newest-first stream of signals, alerts, account scores, and agent
 * runs. Manager-only (it reads the analytics activity endpoint). Auto-refreshes with the page.
 */
function ActivityFeed({ state, live }: { state: AsyncState<ActivityItem[]>; live: boolean }) {
  return (
    <Card padding="md">
      <CardHeader
        title="Live activity"
        subtitle="Signals, scores, alerts, and agent runs across your workspace"
        actions={
          <Badge tone={live ? "success" : "neutral"} dot>
            {live ? "Live" : "Paused"}
          </Badge>
        }
      />
      <DataState
        state={state}
        errorTitle="Couldn't load activity"
        skeleton={<RowsSkeleton rows={6} />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            compact
            icon={<Icons.TrendUpIcon />}
            title="Nothing's happened yet"
            description="As signals land, accounts get scored, and agents run, the stream shows up here."
          />
        }
      >
        {(rows) => (
          <div className={styles.list}>
            {rows.slice(0, 8).map((item) => (
              <div key={item.id} className={styles.item}>
                <Badge className={styles.itemAccent} tone={activityTone(item.tone)} dot>
                  {ACTIVITY_LABELS[item.kind] ?? humanize(item.kind)}
                </Badge>
                <div className={styles.itemBody}>
                  <div className={styles.itemTitle}>{item.title}</div>
                  <div className={styles.itemMeta}>
                    {item.account_name && <span>{item.account_name}</span>}
                    {item.account_name && <span aria-hidden="true">·</span>}
                    {item.detail && <span>{humanize(item.detail)}</span>}
                    {item.detail && <span aria-hidden="true">·</span>}
                    <span>{timeAgo(item.at)}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </DataState>
    </Card>
  );
}

function RowsSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className={styles.list}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className={styles.item}>
          <Skeleton width={44} height={20} radius={999} />
          <div className={styles.itemBody}>
            <Skeleton width="70%" height={12} />
            <div style={{ height: 6 }} />
            <Skeleton width="45%" height={10} />
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * Manager-only attribution: ties logged deal outcomes back to scored accounts and
 * shows the funnel that the relevance weights learn from. Visible to manager+ only.
 */
function OutcomeAttribution({ onTune }: { onTune: () => void }) {
  const api = useApiClient();
  const summary = useApi<OutcomeSummary>((signal) => api.getOutcomeSummary(signal), []);

  return (
    <Card padding="md" className={styles.attribution}>
      <CardHeader
        title="Outcome attribution"
        subtitle="How logged deals trace back to scored accounts"
        actions={
          <Button
            variant="ghost"
            size="sm"
            iconLeft={<Icons.TrendUpIcon />}
            onClick={onTune}
          >
            Tune weights
          </Button>
        }
      />
      <DataState
        state={summary}
        skeleton={
          <div className={styles.attrStats}>
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className={styles.metric}>
                <Skeleton width="60%" height={12} />
                <div style={{ height: 8 }} />
                <Skeleton width="40%" height={22} />
              </div>
            ))}
          </div>
        }
        isEmpty={(data) => data.total === 0}
        empty={
          <EmptyState
            compact
            icon={<Icons.TrophyIcon />}
            title="No outcomes logged yet"
            description="Log a win or loss from any account to start attributing results back to fit scoring."
          />
        }
      >
        {(data) => {
          const won = data.by_stage.won ?? 0;
          const lost = data.by_stage.lost ?? 0;
          const decided = won + lost;
          const winRate = decided > 0 ? won / decided : null;
          const max = Math.max(1, ...FUNNEL.map((s) => data.by_stage[s.key] ?? 0));
          return (
            <>
              <div className={styles.attrStats}>
                <Metric label="Outcomes logged" value={formatNumber(data.total)} />
                <Metric label="Positive signals" value={formatNumber(data.positive)} />
                <Metric label="Closed won" value={formatNumber(won)} tone="success" />
                <Metric
                  label="Win rate"
                  value={winRate == null ? "—" : formatPercent(winRate)}
                  hint={decided > 0 ? `${won}/${decided} decided` : "No closed deals yet"}
                />
              </div>
              <div className={styles.funnel}>
                {FUNNEL.map((stage) => {
                  const count = data.by_stage[stage.key] ?? 0;
                  const pct = Math.round((count / max) * 100);
                  return (
                    <div key={stage.key} className={styles.funnelRow}>
                      <span className={styles.funnelLabel}>{stage.label}</span>
                      <div className={styles.funnelTrack}>
                        <span
                          className={styles.funnelFill}
                          data-stage={stage.key}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span className={styles.funnelCount}>{formatNumber(count)}</span>
                    </div>
                  );
                })}
              </div>
            </>
          );
        }}
      </DataState>
    </Card>
  );
}

function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "success";
}) {
  return (
    <div className={styles.metric}>
      <span className={styles.metricLabel}>{label}</span>
      <span className={styles.metricValue} data-tone={tone}>
        {value}
      </span>
      {hint && <span className={styles.metricHint}>{hint}</span>}
    </div>
  );
}
