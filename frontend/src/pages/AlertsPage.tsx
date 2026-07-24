import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Icons,
  Skeleton,
  Tabs,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { humanize } from "@/lib/format";
import { severityTone, strengthMeta } from "@/lib/display";
import type { Alert, AlertStatus } from "@/lib/types";
import styles from "./AlertsPage.module.css";

/** Read a string field out of an alert's enriched `meta` (empty/non-string -> undefined). */
function metaStr(meta: Record<string, unknown>, key: string): string | undefined {
  const v = meta[key];
  return typeof v === "string" && v.trim() ? v : undefined;
}

/** The actionable intelligence block — only renders when the alert was enriched. */
function AlertIntel({ meta }: { meta: Record<string, unknown> }) {
  const matchedIcp = metaStr(meta, "matched_icp");
  const suggested = metaStr(meta, "suggested_action");
  const nba = metaStr(meta, "next_best_action");
  const sourceUrl = metaStr(meta, "source_url");
  if (!suggested && !matchedIcp && !nba) return null;
  return (
    <div className={styles.intel}>
      {matchedIcp && <div className={styles.intelIcp}>{matchedIcp}</div>}
      {suggested && (
        <div className={styles.intelRow}>
          <span className={styles.intelLabel}>Do now</span> {suggested}
        </div>
      )}
      {nba && (
        <div className={styles.intelRow}>
          <span className={styles.intelLabel}>Next</span> {nba}
        </div>
      )}
      {sourceUrl && (
        <a
          className={styles.intelLink}
          href={sourceUrl}
          target="_blank"
          rel="noreferrer noopener"
          onClick={(e) => e.stopPropagation()}
        >
          View source ↗
        </a>
      )}
    </div>
  );
}

export function AlertsPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [status, setStatus] = useState<AlertStatus>("open");
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const alerts = useApi<Alert[]>((signal) => api.listAlerts(status, signal), [status]);

  async function ack(alert: Alert) {
    setPending((p) => ({ ...p, [alert.id]: true }));
    try {
      await api.ackAlert(alert.id);
      alerts.setData((prev) => (prev ?? []).filter((a) => a.id !== alert.id));
      toast.success("Alert acknowledged", alert.title);
    } catch (err) {
      toast.error(
        "Couldn't acknowledge alert",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setPending((p) => {
        const next = { ...p };
        delete next[alert.id];
        return next;
      });
    }
  }

  return (
    <div>
      <PageHeader
        title="Alerts"
        description="Time-sensitive notifications routed from your most important signals."
        actions={
          <Button
            variant="secondary"
            iconLeft={<Icons.RefreshIcon />}
            onClick={alerts.refetch}
          >
            Refresh
          </Button>
        }
      />

      <div className={styles.tabs}>
        <Tabs
          aria-label="Alert status"
          value={status}
          onChange={(v) => setStatus(v as AlertStatus)}
          items={[
            { value: "open", label: "Open" },
            { value: "acked", label: "Acknowledged" },
          ]}
        />
      </div>

      <DataState
        state={alerts}
        skeleton={
          <div className={styles.list}>
            {Array.from({ length: 4 }).map((_, i) => (
              <Card key={i} padding="md">
                <Skeleton width="50%" height={14} />
                <div style={{ height: 8 }} />
                <Skeleton width="75%" height={11} />
              </Card>
            ))}
          </div>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.BellIcon />}
            title={status === "open" ? "No open alerts" : "Nothing acknowledged yet"}
            description={
              status === "open"
                ? "Alerts fire when a play's trigger matches a signal, or from the daily digest."
                : "Acknowledged alerts will be listed here for reference."
            }
            action={
              status === "open" ? (
                <Button
                  variant="secondary"
                  iconLeft={<Icons.BoltIcon />}
                  onClick={() => navigate("/plays")}
                >
                  Set up a play
                </Button>
              ) : undefined
            }
          />
        }
      >
        {(rows) => (
          <div className={styles.list}>
            {rows.map((alert) => (
              <Card key={alert.id} padding="md">
                <div className={styles.alert}>
                  <div className={styles.body}>
                    <div className={styles.titleRow}>
                      <Badge tone={severityTone(alert.severity)} dot>
                        {alert.severity}
                      </Badge>
                      {metaStr(alert.meta, "category") && (
                        <Badge tone="info">{metaStr(alert.meta, "category")}</Badge>
                      )}
                      <span className={styles.title}>{alert.title}</span>
                      {typeof alert.meta.importance === "number" && (
                        <Badge tone={strengthMeta((alert.meta.importance as number) / 100).tone}>
                          {alert.meta.importance as number}/100
                        </Badge>
                      )}
                    </div>
                    {alert.body && <p className={styles.text}>{alert.body}</p>}
                    <AlertIntel meta={alert.meta} />
                    <div className={styles.meta}>
                      <Icons.SendIcon />
                      <span>{humanize(alert.channel)}</span>
                      <span>·</span>
                      <span>{humanize(alert.source)}</span>
                    </div>
                  </div>
                  <div className={styles.actions}>
                    {alert.account_id && (
                      <Button
                        variant="ghost"
                        onClick={() => navigate(`/accounts/${alert.account_id}`)}
                      >
                        Open account
                      </Button>
                    )}
                    {alert.status === "open" && (
                      <Button
                        variant="secondary"
                        iconLeft={<Icons.CheckIcon />}
                        loading={pending[alert.id]}
                        onClick={() => ack(alert)}
                      >
                        Acknowledge
                      </Button>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </DataState>
    </div>
  );
}
