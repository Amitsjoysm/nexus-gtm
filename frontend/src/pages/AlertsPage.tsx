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
import { severityTone } from "@/lib/display";
import type { Alert, AlertStatus } from "@/lib/types";
import styles from "./AlertsPage.module.css";

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
                      <span className={styles.title}>{alert.title}</span>
                    </div>
                    <p className={styles.text}>{alert.body}</p>
                    <div className={styles.meta}>
                      <Icons.SendIcon />
                      <span>{humanize(alert.channel)}</span>
                      <span>·</span>
                      <span>{humanize(alert.source)}</span>
                    </div>
                  </div>
                  {alert.status === "open" && (
                    <div className={styles.actions}>
                      <Button
                        variant="secondary"
                        iconLeft={<Icons.CheckIcon />}
                        loading={pending[alert.id]}
                        onClick={() => ack(alert)}
                      >
                        Acknowledge
                      </Button>
                    </div>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </DataState>
    </div>
  );
}
