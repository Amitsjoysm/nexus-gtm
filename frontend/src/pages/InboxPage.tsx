import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
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
import type { BadgeTone } from "@/components/ui";
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

/** SLA aging tone: a task that has sat for a day deserves attention; three days is overdue. */
function slaTone(ageHours: number): BadgeTone {
  if (ageHours >= 72) return "danger";
  if (ageHours >= 24) return "warning";
  return "neutral";
}

/** "In queue 3h" / "In queue 2d" — the commitment cue that keeps queues from rotting. */
function slaLabel(ageHours: number): string {
  const rel = formatAgeHours(ageHours);
  return rel === "just now" ? "Just added" : `In queue ${rel.replace(" ago", "")}`;
}

/** True when the keydown originated in a form control, where shortcuts must not fire. */
function isTypingTarget(e: KeyboardEvent): boolean {
  const el = e.target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
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
  const navigate = useNavigate();
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState(0);
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  const inbox = useApi<InboxTask[]>((signal) => api.listInbox(signal), []);
  const rows = inbox.data ?? [];

  const complete = useCallback(
    async (task: InboxTask) => {
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
    },
    [api, inbox, toast],
  );

  const openAccount = useCallback(
    (task: InboxTask) => {
      if (task.account_id) navigate(`/accounts/${task.account_id}`);
    },
    [navigate],
  );

  // Keyboard-first triage: J/K (or arrows) move, E completes, D/Enter opens the account.
  // Reps clear dozens of tasks a day; speed-per-task is the adoption metric that matters.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (isTypingTarget(e) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (rows.length === 0) return;
      const key = e.key.toLowerCase();
      if (key === "j" || key === "arrowdown") {
        e.preventDefault();
        setSelected((i) => Math.min(i + 1, rows.length - 1));
      } else if (key === "k" || key === "arrowup") {
        e.preventDefault();
        setSelected((i) => Math.max(i - 1, 0));
      } else if (key === "e") {
        e.preventDefault();
        const task = rows[Math.min(selected, rows.length - 1)];
        if (task && !pending[task.id]) void complete(task);
      } else if (key === "d" || key === "enter") {
        e.preventDefault();
        const task = rows[Math.min(selected, rows.length - 1)];
        if (task) openAccount(task);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows, selected, pending, complete, openAccount]);

  // Keep the selection valid as tasks complete, and keep the selected row in view.
  useEffect(() => {
    if (selected > rows.length - 1) setSelected(Math.max(0, rows.length - 1));
  }, [rows.length, selected]);
  useEffect(() => {
    const task = rows[selected];
    if (task) rowRefs.current.get(task.id)?.scrollIntoView({ block: "nearest" });
  }, [rows, selected]);

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

      <p className={styles.kbdHint}>
        Keyboard: <kbd>J</kbd>/<kbd>K</kbd> move · <kbd>E</kbd> complete · <kbd>D</kbd> open
        account
      </p>

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
        isEmpty={(items) => items.length === 0}
        empty={
          <EmptyState
            icon={<Icons.InboxIcon />}
            title="You're at inbox zero"
            description="No pending tasks right now. New actions appear here as signals are detected."
          />
        }
      >
        {(items) => (
          <div className={styles.list} role="list" aria-label="Inbox tasks">
            {items.map((task, idx) => {
              const label = suggestionLabel(task.suggested_action);
              const isSelected = idx === selected;
              return (
                <Card
                  key={task.id}
                  padding="md"
                  role="listitem"
                  aria-current={isSelected || undefined}
                  className={isSelected ? styles.selected : undefined}
                  ref={(el: HTMLDivElement | null) => {
                    if (el) rowRefs.current.set(task.id, el);
                    else rowRefs.current.delete(task.id);
                  }}
                  onClick={() => setSelected(idx)}
                >
                  <div className={styles.task}>
                    <div className={styles.main}>
                      <div className={styles.titleRow}>
                        <Badge tone={priorityTone(task.priority)} dot>
                          Priority {task.priority}
                        </Badge>
                        {task.age_hours != null && (
                          <Badge tone={slaTone(task.age_hours)}>{slaLabel(task.age_hours)}</Badge>
                        )}
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
                      {task.account_id && (
                        <Button
                          variant="ghost"
                          iconLeft={<Icons.BuildingIcon />}
                          onClick={() => openAccount(task)}
                        >
                          Open account
                        </Button>
                      )}
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
