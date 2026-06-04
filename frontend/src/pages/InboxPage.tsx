import { useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Icons,
  Skeleton,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatAgeHours, humanize } from "@/lib/format";
import { priorityTone } from "@/lib/display";
import { EMAIL_STATUS_META, asEmailStatus } from "@/lib/runStatus";
import type { InboxTask, TriageSummary } from "@/lib/types";
import styles from "./InboxPage.module.css";

/** Extract a short, human label for a task's suggested action, if present. */
function suggestionLabel(action: Record<string, unknown>): string | null {
  const type = action.type ?? action.action ?? action.kind;
  return typeof type === "string" ? humanize(type) : null;
}

/**
 * One-glance triage cues for a task: how fresh the buying signal is, whether the buyer is
 * reachable, and whether there's enough to ground outreach. Color is always paired with a
 * label so the row reads without relying on hue alone.
 */
function TriageStrip({ triage }: { triage: TriageSummary }) {
  const status = asEmailStatus(triage.deliverability);
  const deliver = status ? EMAIL_STATUS_META[status] : null;
  const hasRecency = triage.signal_kind != null;
  if (!deliver && !hasRecency && !triage.research_ready) return null;

  return (
    <div className={styles.triage}>
      {hasRecency && (
        <span className={styles.triageItem}>
          <Icons.SignalIcon />
          {humanize(triage.signal_kind)} · {formatAgeHours(triage.signal_age_hours)}
        </span>
      )}
      {deliver && (
        <Badge tone={deliver.tone} dot>
          {deliver.label}
        </Badge>
      )}
      {triage.research_ready && (
        <span className={styles.triageItem}>
          <Icons.FileTextIcon />
          Research-ready
        </span>
      )}
    </div>
  );
}

export function InboxPage() {
  const api = useApiClient();
  const toast = useToast();
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const inbox = useApi<InboxTask[]>((signal) => api.listInbox(signal), []);

  async function complete(task: InboxTask) {
    setPending((p) => ({ ...p, [task.id]: true }));
    try {
      await api.completeTask(task.id);
      inbox.setData((prev) => (prev ?? []).filter((t) => t.id !== task.id));
      toast.success("Task completed", task.title);
    } catch (err) {
      toast.error(
        "Couldn't complete task",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setPending((p) => {
        const next = { ...p };
        delete next[task.id];
        return next;
      });
    }
  }

  return (
    <div>
      <PageHeader
        title="Inbox"
        description="Prioritized actions, generated from buying signals across your accounts."
        actions={
          <Button
            variant="secondary"
            iconLeft={<Icons.RefreshIcon />}
            onClick={inbox.refetch}
          >
            Refresh
          </Button>
        }
      />

      <DataState
        state={inbox}
        skeleton={
          <div className={styles.list}>
            {Array.from({ length: 5 }).map((_, i) => (
              <Card key={i} padding="md">
                <Skeleton width="55%" height={14} />
                <div style={{ height: 10 }} />
                <Skeleton width="80%" height={11} />
              </Card>
            ))}
          </div>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.InboxIcon />}
            title="You're at inbox zero"
            description="No pending tasks right now. New actions appear here as signals are detected."
          />
        }
      >
        {(rows) => (
          <div className={styles.list}>
            {rows.map((task) => {
              const label = suggestionLabel(task.suggested_action);
              return (
                <Card key={task.id} padding="md">
                  <div className={styles.task}>
                    <div className={styles.main}>
                      <div className={styles.titleRow}>
                        <Badge tone={priorityTone(task.priority)} dot>
                          Priority {task.priority}
                        </Badge>
                        <span className={styles.title}>{task.title}</span>
                      </div>
                      <p className={styles.reason}>{task.reason}</p>
                      {task.triage && <TriageStrip triage={task.triage} />}
                      {label && (
                        <div className={styles.suggestion}>
                          <Icons.SparklesIcon />
                          <span>Suggested:</span>
                          <code>{label}</code>
                        </div>
                      )}
                    </div>
                    <div className={styles.actions}>
                      <Button
                        variant="secondary"
                        iconLeft={<Icons.CheckIcon />}
                        loading={pending[task.id]}
                        onClick={() => complete(task)}
                      >
                        Complete
                      </Button>
                    </div>
                  </div>
                </Card>
              );
            })}
          </div>
        )}
      </DataState>
    </div>
  );
}
