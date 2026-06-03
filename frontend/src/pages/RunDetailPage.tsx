import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  ErrorState,
  Field,
  Icons,
  Input,
  ScoreMeter,
  Skeleton,
  Spinner,
  Textarea,
  useToast,
} from "@/components/ui";
import { ChatComposer, ChatThread, useChatSession } from "@/components/chat";
import { ResultsPanel } from "@/components/discovery";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { humanize, timeAgo } from "@/lib/format";
import {
  RUN_STATUS_LABEL,
  RUN_STATUS_TONE,
  STEP_STATUS_LABEL,
  STEP_STATUS_TONE,
  goalLabel,
  isRunActive,
  toolLabel,
} from "@/lib/runStatus";
import type { BadgeTone } from "@/components/ui";
import type { Run, RunStep, RunStreamEvent } from "@/lib/types";
import styles from "./RunDetailPage.module.css";

export function RunDetailPage() {
  const { id = "" } = useParams();
  const api = useApiClient();

  const run = useApi<Run>((signal) => api.getRun(id, signal), [id]);
  const { data, setData } = run;
  const active = data ? isRunActive(data.status) : false;

  // Snapshot poll: keep step/blackboard state fresh while the run is live, without the
  // skeleton flash a full refetch() would cause (uses setData, not the loading path).
  useEffect(() => {
    if (!active) return;
    const tick = setInterval(async () => {
      try {
        const fresh = await api.getRun(id);
        setData(fresh);
      } catch {
        /* transient; the next tick (or the stream close) will reconcile */
      }
    }, 2500);
    return () => clearInterval(tick);
  }, [active, api, id, setData]);

  return (
    <div>
      <DataState
        state={run}
        errorTitle="Run not found"
        skeleton={
          <>
            <Skeleton width={120} height={12} />
            <div style={{ height: 12 }} />
            <Skeleton width="45%" height={26} />
          </>
        }
      >
        {(r) => <RunConsole run={r} onUpdate={setData} onRefetch={run.refetch} />}
      </DataState>
    </div>
  );
}

function RunConsole({
  run,
  onUpdate,
  onRefetch,
}: {
  run: Run;
  onUpdate: (r: Run) => void;
  onRefetch: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [cancelling, setCancelling] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const active = isRunActive(run.status);
  const isDiscovery = run.goal === "discover" || !!run.blackboard.discovery;
  const chatSessionId = run.chat_session_id ?? null;

  // The live activity feed reads the same append-only log the backend persists. When the
  // stream closes (run reached a terminal state or parked at the gate) we pull a fresh
  // snapshot so steps/blackboard reflect the final state.
  const onClose = useCallback(() => onRefetch(), [onRefetch]);
  const { events, connected, reconnect } = useRunActivity(run.id, onClose);

  const gateStep = run.steps.find(
    (s) => s.status === "awaiting_approval" && s.approval_id,
  );

  async function cancel() {
    setCancelling(true);
    try {
      const updated = await api.cancelRun(run.id);
      onUpdate(updated);
      toast.success("Run cancelled", "No further steps will execute.");
    } catch (err) {
      toast.error(
        "Couldn't cancel",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setCancelling(false);
    }
  }

  function afterDecision(updated: Run) {
    onUpdate(updated);
    // Re-open the stream: deciding the gate resumes execution server-side.
    reconnect();
  }

  return (
    <>
      <PageHeader
        eyebrow={
          <Link to="/runs" className={styles.back}>
            <Icons.ChevronLeftIcon /> AI Runs
          </Link>
        }
        title={goalLabel(run.goal)}
        description={`Started ${timeAgo(run.created_at)}`}
        actions={
          <div className={styles.headerActions}>
            <Badge tone={RUN_STATUS_TONE[run.status]} dot>
              {RUN_STATUS_LABEL[run.status]}
            </Badge>
            {active && (
              <Button
                variant="secondary"
                iconLeft={<Icons.XIcon />}
                loading={cancelling}
                onClick={cancel}
              >
                Cancel run
              </Button>
            )}
          </div>
        }
      />

      {chatSessionId && (
        <button
          type="button"
          className={styles.dockToggle}
          aria-expanded={chatOpen}
          aria-controls="run-chat-dock"
          onClick={() => setChatOpen((open) => !open)}
        >
          <Icons.MessageIcon />
          Conversation
        </button>
      )}

      <div className={styles.body}>
        <div className={styles.main}>
          {run.error && (
            <div className={styles.banner} role="alert">
              <Icons.AlertTriangleIcon />
              <span>{run.error}</span>
            </div>
          )}

          {gateStep && (
            <ApprovalGate
              approvalId={gateStep.approval_id as string}
              draft={run.blackboard.draft}
              onDecided={afterDecision}
            />
          )}

          {isDiscovery && (
            <section className={styles.col}>
              <h2 className={styles.colTitle}>Matches</h2>
              <ResultsPanel runId={run.id} />
            </section>
          )}

          <div className={styles.grid}>
            <section className={styles.col}>
              <h2 className={styles.colTitle}>Steps</h2>
              <StepTimeline steps={run.steps} />
            </section>

            <section className={styles.col}>
              {!isDiscovery && (
                <>
                  <h2 className={styles.colTitle}>Intelligence</h2>
                  <Intelligence run={run} />
                </>
              )}
              <ActivityFeed
                events={events}
                connected={connected}
                active={active}
                paused={!!gateStep}
              />
            </section>
          </div>
        </div>

        {chatSessionId && (
          <aside
            id="run-chat-dock"
            className={styles.dock}
            data-open={chatOpen}
            aria-label="Run conversation"
          >
            <RunMiniChat sessionId={chatSessionId} />
          </aside>
        )}
      </div>
    </>
  );
}

/* ------------------------------- mini-chat ------------------------------- */

/**
 * Docked orchestrator conversation for a run. Shares ChatThread/ChatComposer/useChatSession
 * with the full ChatPage, so the run console and `/orchestrator` never drift apart.
 */
function RunMiniChat({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const toast = useToast();
  const { messages, loading, error, sending, streaming, send, reload } =
    useChatSession(sessionId);
  const [draft, setDraft] = useState("");

  async function handleSend(text: string) {
    try {
      await send(text);
    } catch (err) {
      toast.error(
        "Couldn't send your message",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    }
  }

  if (loading) {
    return (
      <div className={styles.miniChat}>
        <div className={styles.miniHead}>
          <span className={styles.miniTitle}>Conversation</span>
        </div>
        <div className={styles.miniSkeleton}>
          <Skeleton width="60%" height={40} />
          <Skeleton width="75%" height={56} />
          <Skeleton width="50%" height={40} />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.miniChat}>
        <div className={styles.miniHead}>
          <span className={styles.miniTitle}>Conversation</span>
        </div>
        <div className={styles.miniSkeleton}>
          <ErrorState
            compact
            title="Couldn't load the conversation"
            message={error.detail}
            onRetry={reload}
          />
        </div>
      </div>
    );
  }

  return (
    <div className={styles.miniChat}>
      <div className={styles.miniHead}>
        <span className={styles.miniTitle}>Conversation</span>
        {streaming && (
          <span className={styles.miniLive} aria-label="Live updates connected">
            <span className={styles.miniLiveDot} aria-hidden="true" />
            Live
          </span>
        )}
        <Link to={`/orchestrator/${sessionId}`} className={styles.miniExpand}>
          Open full view
        </Link>
      </div>
      <ChatThread
        messages={messages}
        onChip={(text) => setDraft(text)}
        onOpenRun={(runId) => navigate(`/runs/${runId}`)}
      />
      <div className={styles.miniComposer}>
        <ChatComposer
          value={draft}
          onValueChange={setDraft}
          onSend={handleSend}
          busy={sending}
        />
      </div>
    </div>
  );
}

/* ----------------------------- step timeline ----------------------------- */

function StepTimeline({ steps }: { steps: RunStep[] }) {
  return (
    <ol className={styles.timeline}>
      {steps.map((step) => {
        const running = step.status === "running";
        return (
          <li key={step.idx} className={styles.step} data-status={step.status}>
            <span className={styles.stepDot} aria-hidden="true">
              {running ? <Spinner size={14} /> : <StepGlyph status={step.status} />}
            </span>
            <div className={styles.stepBody}>
              <div className={styles.stepTop}>
                <span className={styles.stepName}>{toolLabel(step.tool)}</span>
                <Badge tone={STEP_STATUS_TONE[step.status]} dot>
                  {STEP_STATUS_LABEL[step.status]}
                </Badge>
              </div>
              <div className={styles.stepMeta}>
                {step.requires_approval && <span>Approval gate</span>}
                {step.attempts > 1 && <span>{step.attempts} attempts</span>}
                {step.depends_on.length > 0 && (
                  <span>after step {step.depends_on.map((d) => d + 1).join(", ")}</span>
                )}
              </div>
              {step.error && (
                <p className={styles.stepError} role="alert">
                  {step.error}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function StepGlyph({ status }: { status: RunStep["status"] }) {
  if (status === "completed") return <Icons.CheckIcon width={13} height={13} />;
  if (status === "failed" || status === "rejected") return <Icons.XIcon width={13} height={13} />;
  if (status === "awaiting_approval") return <Icons.ShieldCheckIcon width={13} height={13} />;
  return <span className={styles.stepDotInner} />;
}

/* --------------------------- intelligence panel -------------------------- */

function Intelligence({ run }: { run: Run }) {
  const { research, composite, draft } = run.blackboard;
  const facts = Array.isArray(research?.facts) ? (research?.facts as unknown[]) : [];
  const sources = Array.isArray(research?.sources) ? (research?.sources as unknown[]) : [];
  const hasResearch = !!research?.brief || facts.length > 0;
  const hasDraft = !!draft && (!!draft.subject || !!draft.body || !!draft.message);

  if (!hasResearch && typeof composite !== "number" && !hasDraft) {
    return (
      <Card padding="lg">
        <p className={styles.placeholder}>
          Agent output lands here as the run progresses — research brief, fit score, and the
          drafted message.
        </p>
      </Card>
    );
  }

  return (
    <div className={styles.panels}>
      {typeof composite === "number" && (
        <Card padding="lg">
          <div className={styles.scoreRow}>
            <div>
              <div className={styles.panelLabel}>Fit score</div>
              <div className={styles.scoreValue}>{Math.round(composite)}</div>
            </div>
            <ScoreMeter value={composite} />
          </div>
        </Card>
      )}

      {hasResearch && (
        <Card padding="lg">
          <div className={styles.panelLabel}>Research brief</div>
          {research?.brief && <p className={styles.brief}>{research.brief}</p>}
          {facts.length > 0 && (
            <ul className={styles.facts}>
              {facts.map((f, i) => (
                <li key={i}>{renderFact(f)}</li>
              ))}
            </ul>
          )}
          {sources.length > 0 && (
            <div className={styles.sources}>
              {sources.map((s, i) => renderSource(s, i))}
            </div>
          )}
        </Card>
      )}

      {hasDraft && draft && (
        <Card padding="lg">
          <div className={styles.draftHead}>
            <div className={styles.panelLabel}>Drafted message</div>
            {draft.grounded && <Badge tone="success">Grounded</Badge>}
          </div>
          {draft.subject && (
            <div className={styles.subject}>
              <span className={styles.subjectLabel}>Subject</span>
              {draft.subject}
            </div>
          )}
          <pre className={styles.body}>{draft.body || draft.message}</pre>
        </Card>
      )}
    </div>
  );
}

function renderFact(fact: unknown): ReactNode {
  if (typeof fact === "string") return fact;
  if (fact && typeof fact === "object") {
    const o = fact as Record<string, unknown>;
    if (typeof o.text === "string") return o.text;
    if (typeof o.fact === "string") return o.fact;
  }
  return String(fact);
}

function renderSource(source: unknown, key: number): ReactNode {
  const o = (source && typeof source === "object" ? source : {}) as Record<string, unknown>;
  const url = typeof o.url === "string" ? o.url : null;
  const title = typeof o.title === "string" ? o.title : url ?? "Source";
  if (!url) return null;
  return (
    <a key={key} className={styles.source} href={url} target="_blank" rel="noreferrer">
      <Icons.ExternalLinkIcon width={13} height={13} />
      {title}
    </a>
  );
}

/* ------------------------------ approval gate ---------------------------- */

function ApprovalGate({
  approvalId,
  draft,
  onDecided,
}: {
  approvalId: string;
  draft: Run["blackboard"]["draft"];
  onDecided: (run: Run) => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [subject, setSubject] = useState(draft?.subject ?? "");
  const [body, setBody] = useState(draft?.body ?? draft?.message ?? "");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    try {
      const edits =
        decision === "approve" && editing
          ? { subject: subject.trim(), body: body.trim() }
          : undefined;
      const updated = await api.decideApproval(approvalId, { decision, edits });
      toast.success(
        decision === "approve" ? "Approved" : "Rejected",
        decision === "approve"
          ? "The message was sent to the sequence."
          : "The send was skipped.",
      );
      onDecided(updated);
    } catch (err) {
      toast.error(
        "Couldn't record your decision",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card padding="lg" className={styles.gate}>
      <div className={styles.gateHead}>
        <span className={styles.gateIcon} aria-hidden="true">
          <Icons.ShieldCheckIcon />
        </span>
        <div>
          <h2 className={styles.gateTitle}>Approval required</h2>
          <p className={styles.gateDesc}>
            Review the drafted outreach. Nothing is sent until you approve.
          </p>
        </div>
      </div>

      <div className={styles.gateDraft}>
        {editing ? (
          <>
            <Field label="Subject">
              <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
            </Field>
            <Field label="Message">
              <Textarea rows={8} value={body} onChange={(e) => setBody(e.target.value)} />
            </Field>
          </>
        ) : (
          <>
            {(draft?.subject || subject) && (
              <div className={styles.subject}>
                <span className={styles.subjectLabel}>Subject</span>
                {subject || draft?.subject}
              </div>
            )}
            <pre className={styles.body}>{body || draft?.body || draft?.message}</pre>
          </>
        )}
      </div>

      <div className={styles.gateActions}>
        <Button
          variant="ghost"
          iconLeft={<Icons.FileTextIcon />}
          onClick={() => setEditing((v) => !v)}
          disabled={busy !== null}
        >
          {editing ? "Preview" : "Edit draft"}
        </Button>
        <div className={styles.gateActionsRight}>
          <Button
            variant="secondary"
            iconLeft={<Icons.XIcon />}
            loading={busy === "reject"}
            disabled={busy === "approve"}
            onClick={() => decide("reject")}
          >
            Reject
          </Button>
          <Button
            iconLeft={<Icons.SendIcon />}
            loading={busy === "approve"}
            disabled={busy === "reject"}
            onClick={() => decide("approve")}
          >
            Approve &amp; send
          </Button>
        </div>
      </div>
    </Card>
  );
}

/* ------------------------------ activity feed ---------------------------- */

function ActivityFeed({
  events,
  connected,
  active,
  paused,
}: {
  events: RunStreamEvent[];
  connected: boolean;
  active: boolean;
  paused: boolean;
}) {
  if (events.length === 0 && !active) return null;
  // Parked at the gate the stream is intentionally closed (waiting on a human), so don't
  // claim we're reconnecting — say it's paused.
  const status = paused ? "Paused for approval" : connected ? "Streaming" : "Reconnecting";
  return (
    <Card padding="lg">
      <div className={styles.feedHead}>
        <div className={styles.panelLabel}>Live activity</div>
        {active && (
          <span className={styles.feedStatus} data-on={connected && !paused}>
            <span className={styles.pulse} aria-hidden="true" />
            {status}
          </span>
        )}
      </div>
      {events.length === 0 ? (
        <p className={styles.placeholder}>Waiting for the first event…</p>
      ) : (
        <ul className={styles.feed}>
          {events.map((ev, i) => (
            <li key={`${ev.seq}-${i}`} className={styles.feedItem}>
              <span className={styles.feedDot} data-tone={eventTone(ev.type)} aria-hidden="true" />
              <span className={styles.feedText}>{eventLabel(ev)}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function eventTone(type: string): BadgeTone {
  if (type.includes("completed") || type.includes("approved")) return "success";
  if (type.includes("failed") || type.includes("rejected") || type.includes("cancel"))
    return "danger";
  if (type.includes("approval") || type.includes("await")) return "warning";
  return "info";
}

function eventLabel(ev: RunStreamEvent): string {
  const tool = ev.data.tool;
  const toolPart = typeof tool === "string" ? `: ${toolLabel(tool)}` : "";
  return `${humanize(ev.type.replace(/\./g, " "))}${toolPart}`;
}

/* ------------------------------ activity hook ---------------------------- */

/**
 * Subscribe to a run's SSE event log. The stream is best-effort: on close (terminal run or
 * parked gate) `onClose` fires so the caller can reconcile the snapshot. `reconnect()` forces
 * a fresh subscription after an approval decision resumes execution. Resumes from the last
 * seen `seq` so reconnects don't replay the whole log.
 */
function useRunActivity(runId: string, onClose: () => void) {
  const api = useApiClient();
  const [events, setEvents] = useState<RunStreamEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const lastSeqRef = useRef(0);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setConnected(true);

    api
      .streamRunEvents(
        runId,
        {
          lastEventId: lastSeqRef.current || undefined,
          onEvent: (ev) => {
            if (ev.seq) lastSeqRef.current = Math.max(lastSeqRef.current, ev.seq);
            setEvents((prev) => [...prev, ev]);
          },
        },
        controller.signal,
      )
      .then(() => {
        if (cancelled) return;
        setConnected(false);
        onCloseRef.current();
      })
      .catch(() => {
        if (!cancelled) setConnected(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [api, runId, epoch]);

  const reconnect = useCallback(() => setEpoch((e) => e + 1), []);
  return { events, connected, reconnect };
}
