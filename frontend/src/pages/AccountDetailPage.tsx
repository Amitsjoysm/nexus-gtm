import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  Spinner,
  Tabs,
  TabPanel,
  Textarea,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber, formatPercent, humanize, timeAgo } from "@/lib/format";
import { strengthMeta } from "@/lib/display";
import type {
  AgentRunResponse,
  Account,
  Contact,
  Lookalike,
  OutcomeStage,
  Role,
  SignalEvent,
} from "@/lib/types";
import styles from "./AccountDetailPage.module.css";

type Tab = "overview" | "contacts" | "signals" | "lookalikes" | "actions";

interface LookalikeState {
  loading: boolean;
  data: Lookalike[] | null;
  error: string | null;
}

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

/** Sales-progression stages a rep can log against an account, ordered by depth. */
const OUTCOME_LABELS: Record<OutcomeStage, string> = {
  sent: "Outreach sent",
  replied: "Got a reply",
  meeting: "Booked a meeting",
  won: "Closed won",
  lost: "Closed lost",
};
const OUTCOME_OPTIONS = (Object.keys(OUTCOME_LABELS) as OutcomeStage[]).map((value) => ({
  value,
  label: OUTCOME_LABELS[value],
}));

interface AgentState {
  loading: boolean;
  result: AgentRunResponse | null;
  error: string | null;
}

export function AccountDetailPage() {
  const { id = "" } = useParams();
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const { session } = useAuth();
  const [tab, setTab] = useState<Tab>("overview");
  const [running, setRunning] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [agents, setAgents] = useState<Record<string, AgentState>>({});
  const [question, setQuestion] = useState("");
  const [lookalikes, setLookalikes] = useState<LookalikeState>({
    loading: false,
    data: null,
    error: null,
  });
  const [outcomeOpen, setOutcomeOpen] = useState(false);
  const [outcomeStage, setOutcomeStage] = useState<OutcomeStage>("won");
  const [outcomeNote, setOutcomeNote] = useState("");
  const [recording, setRecording] = useState(false);

  const canOrchestrate = session ? ROLE_RANK[session.role] >= ROLE_RANK.manager : false;

  const account = useApi<Account>((signal) => api.getAccount(id, signal), [id]);
  const contacts = useApi<Contact[]>((signal) => api.listContacts(id, signal), [id]);

  // Waterfall enrichment for one contact (email + confidence), inline from the roster.
  const [enriching, setEnriching] = useState<Record<string, boolean>>({});
  async function enrichOne(c: Contact) {
    setEnriching((p) => ({ ...p, [c.id]: true }));
    try {
      const updated = await api.enrichContact(c.id);
      contacts.setData((prev) => (prev ?? []).map((x) => (x.id === updated.id ? updated : x)));
      toast.success("Contact enriched", updated.email ?? updated.full_name);
    } catch (err) {
      toast.error(
        "Couldn't enrich contact",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setEnriching((p) => {
        const next = { ...p };
        delete next[c.id];
        return next;
      });
    }
  }
  const signals = useApi<SignalEvent[]>(
    (signal) => api.listSignals({ account_id: id, limit: 50 }, signal),
    [id],
  );

  async function runAgent(name: string, inputs: Record<string, unknown> = {}) {
    setAgents((s) => ({
      ...s,
      [name]: { loading: true, result: s[name]?.result ?? null, error: null },
    }));
    try {
      const res = await api.runAgent(name, id, inputs);
      if (res.status !== "completed" || res.error) {
        throw new ApiError(0, res.error || "The agent could not complete.");
      }
      setAgents((s) => ({ ...s, [name]: { loading: false, result: res, error: null } }));
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : "Please try again.";
      setAgents((s) => ({ ...s, [name]: { loading: false, result: null, error: msg } }));
      toast.error("Agent failed", msg);
    }
  }

  function askQuestion(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q) return;
    runAgent("qa", { question: q });
  }

  async function askOrchestrator(acc: Account) {
    setLaunching(true);
    try {
      const { session: chat } = await api.createChatSession({ account_id: acc.id });
      navigate(`/orchestrator/${chat.id}`);
    } catch (err) {
      toast.error(
        "Couldn't start the conversation",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
      setLaunching(false);
    }
  }

  async function syncToCrm(acc: Account) {
    setSyncing(true);
    try {
      const res = await api.crmPush(acc.id);
      toast.success(
        "Synced to CRM",
        `${acc.name} and ${res.contacts} contact${res.contacts === 1 ? "" : "s"} written back.`,
      );
    } catch (err) {
      toast.error(
        "Couldn't sync to CRM",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSyncing(false);
    }
  }

  async function runLookalikes() {
    setLookalikes({ loading: true, data: null, error: null });
    try {
      const res = await api.findLookalikes(id, 10);
      setLookalikes({ loading: false, data: res.lookalikes, error: null });
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : "Please try again.";
      setLookalikes({ loading: false, data: null, error: msg });
      toast.error("Couldn't find lookalikes", msg);
    }
  }

  async function runPipeline() {
    setRunning(true);
    try {
      await api.runPipeline(id);
      toast.success("Pipeline complete", "Enrichment and scoring refreshed.");
      account.refetch();
      contacts.refetch();
      signals.refetch();
    } catch (err) {
      toast.error(
        "Pipeline failed",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setRunning(false);
    }
  }

  async function recordOutcome(e: FormEvent) {
    e.preventDefault();
    setRecording(true);
    try {
      const note = outcomeNote.trim();
      await api.recordOutcome({
        stage: outcomeStage,
        account_id: id,
        meta: note ? { note } : undefined,
      });
      toast.success(
        "Outcome logged",
        `Recorded "${OUTCOME_LABELS[outcomeStage]}". Wins tune your fit scoring over time.`,
      );
      setOutcomeOpen(false);
      setOutcomeNote("");
      setOutcomeStage("won");
    } catch (err) {
      toast.error(
        "Couldn't log outcome",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setRecording(false);
    }
  }

  return (
    <div>
      <DataState
        state={account}
        errorTitle="Account not found"
        skeleton={
          <>
            <Skeleton width={120} height={12} />
            <div style={{ height: 12 }} />
            <Skeleton width="40%" height={26} />
          </>
        }
      >
        {(acc) => (
          <PageHeader
            eyebrow={
              <Link to="/accounts" className={styles.back}>
                <Icons.ChevronLeftIcon /> Accounts
              </Link>
            }
            title={acc.name}
            description={
              <span className={styles.headMeta}>
                {acc.domain && <span>{acc.domain}</span>}
                {acc.crm_synced_at ? (
                  <Badge tone="success" dot>
                    Synced to {humanize(acc.crm_source ?? "CRM")} · {timeAgo(acc.crm_synced_at)}
                  </Badge>
                ) : acc.crm_source ? (
                  <Badge tone="neutral" dot>
                    From {humanize(acc.crm_source)} · not pushed yet
                  </Badge>
                ) : null}
              </span>
            }
            actions={
              <>
                {canOrchestrate && (
                  <Button
                    variant="secondary"
                    iconLeft={<Icons.MessageIcon />}
                    loading={launching}
                    onClick={() => askOrchestrator(acc)}
                    aria-label={`Ask the orchestrator about ${acc.name}`}
                  >
                    Ask the orchestrator
                  </Button>
                )}
                <Button
                  variant="secondary"
                  iconLeft={<Icons.TrophyIcon />}
                  onClick={() => setOutcomeOpen(true)}
                  aria-label={`Log a deal outcome for ${acc.name}`}
                >
                  Log outcome
                </Button>
                <Button
                  variant="secondary"
                  iconLeft={<Icons.PlugIcon />}
                  loading={syncing}
                  onClick={() => syncToCrm(acc)}
                  aria-label={`Sync ${acc.name} to the CRM`}
                >
                  Sync to CRM
                </Button>
                <Button
                  iconLeft={<Icons.SparklesIcon />}
                  loading={running}
                  onClick={runPipeline}
                >
                  Run pipeline
                </Button>
              </>
            }
          />
        )}
      </DataState>

      <Tabs
        aria-label="Account sections"
        value={tab}
        onChange={(v) => setTab(v as Tab)}
        items={[
          { value: "overview", label: "Overview" },
          { value: "contacts", label: "Contacts", count: contacts.data?.length },
          { value: "signals", label: "Signals", count: signals.data?.length },
          { value: "lookalikes", label: "Lookalikes" },
          { value: "actions", label: "AI Actions" },
        ]}
      />

      <TabPanel id="panel-overview" active={tab === "overview"}>
        <div className={styles.panel}>
          <DataState
            state={account}
            skeleton={
              <Card padding="lg">
                <Skeleton width="100%" height={64} />
              </Card>
            }
          >
            {(acc) => (
              <Card padding="lg">
                <div className={styles.facts}>
                  <Fact label="Industry" value={acc.industry ?? "—"} />
                  <Fact label="Employees" value={formatNumber(acc.employee_count)} />
                  <Fact label="Country" value={acc.country ?? "—"} />
                  <Fact label="Domain" value={acc.domain ?? "—"} />
                </div>
                {acc.tech_stack.length > 0 && (
                  <>
                    <div style={{ height: "var(--space-5)" }} />
                    <div className={styles.factLabel}>Tech stack</div>
                    <div className={styles.tech}>
                      {acc.tech_stack.map((t) => (
                        <Badge key={t} tone="neutral">
                          {t}
                        </Badge>
                      ))}
                    </div>
                  </>
                )}
              </Card>
            )}
          </DataState>
        </div>
      </TabPanel>

      <TabPanel id="panel-contacts" active={tab === "contacts"}>
        <div className={styles.panel}>
          <Card padding="md">
            <DataState
              state={contacts}
              skeleton={<Skeleton width="100%" height={120} />}
              isEmpty={(rows) => rows.length === 0}
              empty={
                <EmptyState
                  compact
                  icon={<Icons.UsersIcon />}
                  title="No contacts yet"
                  description="Run the pipeline to enrich this account with decision-makers."
                  action={
                    <Button
                      variant="secondary"
                      iconLeft={<Icons.SparklesIcon />}
                      loading={running}
                      onClick={runPipeline}
                    >
                      Run pipeline
                    </Button>
                  }
                />
              }
            >
              {(rows) =>
                rows.map((c) => (
                  <div key={c.id} className={styles.contact}>
                    <div className={styles.contactBody}>
                      <div className={styles.contactName}>{c.full_name}</div>
                      <div className={styles.contactMeta}>
                        {[c.title, c.seniority].filter(Boolean).join(" · ") || "—"}
                      </div>
                    </div>
                    <div className={styles.contactRight}>
                      {c.email ? (
                        <a className={styles.link} href={`mailto:${c.email}`}>
                          {c.email}
                        </a>
                      ) : (
                        "—"
                      )}
                      <div>
                        {c.email
                          ? `${formatPercent(c.email_confidence)} confidence`
                          : null}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      loading={enriching[c.id]}
                      onClick={() => enrichOne(c)}
                      aria-label={`Enrich ${c.full_name}`}
                    >
                      Enrich
                    </Button>
                  </div>
                ))
              }
            </DataState>
          </Card>
        </div>
      </TabPanel>

      <TabPanel id="panel-signals" active={tab === "signals"}>
        <div className={styles.panel}>
          <Card padding="md">
            <DataState
              state={signals}
              skeleton={<Skeleton width="100%" height={120} />}
              isEmpty={(rows) => rows.length === 0}
              empty={
                <EmptyState
                  compact
                  icon={<Icons.SignalIcon />}
                  title="No signals yet"
                  description="Buying signals for this account will appear here."
                />
              }
            >
              {(rows) =>
                rows.map((sig) => {
                  const meta = strengthMeta(sig.strength);
                  return (
                    <div key={sig.id} className={styles.signal}>
                      <Badge tone={meta.tone} dot>
                        {meta.label}
                      </Badge>
                      <div className={styles.signalBody}>
                        <div className={styles.signalTitle}>{sig.title}</div>
                        {sig.body && <div className={styles.signalText}>{sig.body}</div>}
                        <div className={styles.signalMeta}>
                          <span>{humanize(sig.kind)}</span>
                          <span>·</span>
                          <span>{sig.source}</span>
                          <span>·</span>
                          <span>{timeAgo(sig.occurred_at)}</span>
                          {sig.url && (
                            <>
                              <span>·</span>
                              <a
                                className={styles.link}
                                href={sig.url}
                                target="_blank"
                                rel="noreferrer"
                              >
                                Source
                              </a>
                            </>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })
              }
            </DataState>
          </Card>
        </div>
      </TabPanel>

      <TabPanel id="panel-lookalikes" active={tab === "lookalikes"}>
        <div className={styles.panel}>
          <Card padding="lg">
            <div className={styles.actionHead}>
              <span className={styles.actionIcon} aria-hidden="true">
                <Icons.TargetIcon />
              </span>
              <div>
                <h3 className={styles.actionTitle}>Find similar companies</h3>
                <p className={styles.actionDesc}>
                  Seed from this account's domain to surface lookalikes, ranked by ICP fit.
                </p>
              </div>
            </div>
            <div>
              <Button
                variant={lookalikes.data ? "secondary" : "primary"}
                iconLeft={lookalikes.data ? <Icons.RefreshIcon /> : <Icons.TargetIcon />}
                loading={lookalikes.loading}
                onClick={runLookalikes}
              >
                {lookalikes.data ? "Refresh" : "Find lookalikes"}
              </Button>
            </div>
            <LookalikeResult state={lookalikes} />
          </Card>
        </div>
      </TabPanel>

      <TabPanel id="panel-actions" active={tab === "actions"}>
        <div className={styles.panel}>
          <div className={styles.actionsGrid}>
            <ActionCard
              icon={<Icons.FileTextIcon />}
              title="Research brief"
              description="Summarize what matters about this account, grounded in your ICP."
              state={agents.research}
              runLabel="Generate brief"
              onRun={() => runAgent("research")}
            >
              {(out) => <ResearchResult output={out} />}
            </ActionCard>

            <ActionCard
              icon={<Icons.MessageIcon />}
              title="Draft outreach"
              description="Write a personalized first email using the strongest signal as the hook."
              state={agents.messaging}
              runLabel="Draft message"
              onRun={() => runAgent("messaging")}
            >
              {(out) => <MessagingResult output={out} onCopy={copyToClipboard} />}
            </ActionCard>

            <ActionCard
              icon={<Icons.UserCheckIcon />}
              title="Recommended contacts"
              description="Rank known contacts by fit to the buying committee."
              state={agents.contact_rec}
              runLabel="Rank contacts"
              onRun={() => runAgent("contact_rec")}
            >
              {(out) => <ContactRecResult output={out} />}
            </ActionCard>

            <Card padding="lg" className={styles.actionCard}>
              <div className={styles.actionHead}>
                <span className={styles.actionIcon} aria-hidden="true">
                  <Icons.HelpCircleIcon />
                </span>
                <div>
                  <h3 className={styles.actionTitle}>Ask about this account</h3>
                  <p className={styles.actionDesc}>
                    Get a grounded answer from signals, contacts, and firmographics.
                  </p>
                </div>
              </div>
              <form className={styles.askForm} onSubmit={askQuestion}>
                <Field label="Your question" hideLabel>
                  <Input
                    value={question}
                    onChange={(e) => setQuestion(e.target.value)}
                    placeholder="e.g. Why is this account a good fit right now?"
                  />
                </Field>
                <Button
                  type="submit"
                  iconLeft={<Icons.SparklesIcon />}
                  loading={agents.qa?.loading}
                  disabled={!question.trim()}
                >
                  Ask
                </Button>
              </form>
              <AgentResult state={agents.qa}>
                {(out) => <QAResult output={out} />}
              </AgentResult>
            </Card>
          </div>
        </div>
      </TabPanel>

      <Modal
        open={outcomeOpen}
        onClose={() => setOutcomeOpen(false)}
        title="Log a deal outcome"
        description="Record what happened with this account. Wins feed back into your relevance weights, sharpening fit scoring across the workspace."
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setOutcomeOpen(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="outcome-form"
              iconLeft={<Icons.CheckIcon />}
              loading={recording}
            >
              Save outcome
            </Button>
          </>
        }
      >
        <form id="outcome-form" className={styles.outcomeForm} onSubmit={recordOutcome}>
          <Field label="Outcome">
            <Select
              options={OUTCOME_OPTIONS}
              value={outcomeStage}
              onChange={(e) => setOutcomeStage(e.target.value as OutcomeStage)}
            />
          </Field>
          <Field label="Note" hint="Optional. Context for your team — what closed or stalled the deal.">
            <Textarea
              rows={3}
              value={outcomeNote}
              onChange={(e) => setOutcomeNote(e.target.value)}
              placeholder="e.g. Won on the analytics use case; champion was the VP of RevOps."
            />
          </Field>
        </form>
      </Modal>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.fact}>
      <span className={styles.factLabel}>{label}</span>
      <span className={styles.factValue}>{value}</span>
    </div>
  );
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** A single agent action: header, run button, and a result region that owns the four states. */
function ActionCard({
  icon,
  title,
  description,
  runLabel,
  onRun,
  state,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  runLabel: string;
  onRun: () => void;
  state: AgentState | undefined;
  children: (output: Record<string, unknown>) => React.ReactNode;
}) {
  const hasResult = !!state?.result;
  return (
    <Card padding="lg" className={styles.actionCard}>
      <div className={styles.actionHead}>
        <span className={styles.actionIcon} aria-hidden="true">
          {icon}
        </span>
        <div>
          <h3 className={styles.actionTitle}>{title}</h3>
          <p className={styles.actionDesc}>{description}</p>
        </div>
      </div>
      <div>
        <Button
          variant={hasResult ? "secondary" : "primary"}
          iconLeft={hasResult ? <Icons.RefreshIcon /> : <Icons.SparklesIcon />}
          loading={state?.loading}
          onClick={onRun}
        >
          {hasResult ? "Regenerate" : runLabel}
        </Button>
      </div>
      <AgentResult state={state}>{children}</AgentResult>
    </Card>
  );
}

/** Renders the loading / error / success states for one agent run. */
function AgentResult({
  state,
  children,
}: {
  state: AgentState | undefined;
  children: (output: Record<string, unknown>) => React.ReactNode;
}) {
  if (!state) return null;
  if (state.loading && !state.result) {
    return (
      <div className={styles.agentLoading}>
        <Spinner size={16} /> Generating…
      </div>
    );
  }
  if (state.error) {
    return (
      <div className={styles.agentError} role="alert">
        {state.error}
      </div>
    );
  }
  if (!state.result) return null;
  return <div className={styles.agentOutput}>{children(state.result.output)}</div>;
}

/** Loading / error / empty / list states for a lookalike search. */
function LookalikeResult({ state }: { state: LookalikeState }) {
  if (state.loading) {
    return (
      <div className={styles.agentLoading}>
        <Spinner size={16} /> Searching for similar companies…
      </div>
    );
  }
  if (state.error) {
    return (
      <div className={styles.agentError} role="alert">
        {state.error}
      </div>
    );
  }
  if (state.data === null) return null;
  if (state.data.length === 0) {
    return (
      <p className={styles.agentNote}>
        No lookalikes found. Similarity search needs a configured provider (Exa); offline this
        stays empty.
      </p>
    );
  }
  return (
    <ul className={styles.recList}>
      {state.data.map((lk) => {
        const meta = strengthMeta(lk.score / 100);
        return (
          <li key={lk.domain} className={styles.rec}>
            <div className={styles.recTop}>
              <div>
                <span className={styles.recName}>{lk.name}</span>
                <span className={styles.recTitle}>
                  {lk.url ? (
                    <a className={styles.link} href={lk.url} target="_blank" rel="noreferrer">
                      {lk.domain}
                    </a>
                  ) : (
                    lk.domain
                  )}
                </span>
              </div>
              <div className={styles.recBadges}>
                {lk.already_tracked ? <Badge tone="neutral">Tracked</Badge> : null}
                <Badge tone={meta.tone} dot>
                  Fit {lk.score}
                </Badge>
              </div>
            </div>
            {lk.snippet && <p className={styles.recWhy}>{lk.snippet}</p>}
          </li>
        );
      })}
    </ul>
  );
}

function ResearchResult({ output }: { output: Record<string, unknown> }) {
  const brief = typeof output.brief === "string" ? output.brief : "";
  const facts = Array.isArray(output.facts) ? (output.facts as string[]) : [];
  const sources = Array.isArray(output.sources)
    ? (output.sources as { title?: string; url?: string }[])
    : [];
  return (
    <div className={styles.resultStack}>
      {brief && <p className={styles.briefText}>{brief}</p>}
      {facts.length > 0 && (
        <ul className={styles.factsList}>
          {facts.map((f, i) => (
            <li key={i}>{f}</li>
          ))}
        </ul>
      )}
      {sources.length > 0 && (
        <div className={styles.sources}>
          {sources.map((s, i) =>
            s.url ? (
              <a key={i} className={styles.link} href={s.url} target="_blank" rel="noreferrer">
                {s.title || s.url}
              </a>
            ) : null,
          )}
        </div>
      )}
    </div>
  );
}

function MessagingResult({
  output,
  onCopy,
}: {
  output: Record<string, unknown>;
  onCopy: (text: string) => Promise<boolean>;
}) {
  const toast = useToast();
  const subject = typeof output.subject === "string" ? output.subject : "";
  const body = typeof output.body === "string" ? output.body : "";
  const message = typeof output.message === "string" ? output.message : "";
  const full = subject ? `Subject: ${subject}\n\n${body}` : body || message;
  return (
    <div className={styles.email}>
      {subject && (
        <div className={styles.emailSubject}>
          <span className={styles.emailLabel}>Subject</span> {subject}
        </div>
      )}
      <pre className={styles.emailBody}>{body || message}</pre>
      <Button
        variant="ghost"
        size="sm"
        iconLeft={<Icons.SendIcon />}
        onClick={async () => {
          const ok = await onCopy(full);
          if (ok) toast.success("Copied", "Draft copied to your clipboard.");
          else toast.error("Couldn't copy", "Select and copy the text manually.");
        }}
      >
        Copy draft
      </Button>
    </div>
  );
}

function ContactRecResult({ output }: { output: Record<string, unknown> }) {
  const recs = Array.isArray(output.recommendations)
    ? (output.recommendations as Record<string, unknown>[])
    : [];
  const note = typeof output.note === "string" ? output.note : null;
  if (recs.length === 0) {
    return (
      <p className={styles.agentNote}>{note || "No contacts to rank yet. Run the pipeline first."}</p>
    );
  }
  return (
    <ul className={styles.recList}>
      {recs.map((r, i) => {
        const score = typeof r.score === "number" ? r.score : 0;
        const meta = strengthMeta(score);
        return (
          <li key={(r.contact_id as string) ?? i} className={styles.rec}>
            <div className={styles.recTop}>
              <div>
                <span className={styles.recName}>{String(r.name ?? "Unknown")}</span>
                <span className={styles.recTitle}>
                  {[r.title, humanize(String(r.seniority ?? ""))].filter(Boolean).join(" · ") || "—"}
                </span>
              </div>
              <div className={styles.recBadges}>
                {r.reachable ? <Badge tone="success">Reachable</Badge> : null}
                <Badge tone={meta.tone} dot>
                  Fit {Math.round(score * 100)}
                </Badge>
              </div>
            </div>
            {typeof r.why === "string" && r.why && <p className={styles.recWhy}>{r.why}</p>}
          </li>
        );
      })}
    </ul>
  );
}

function QAResult({ output }: { output: Record<string, unknown> }) {
  const answer = typeof output.answer === "string" ? output.answer : "";
  const grounded = typeof output.grounded_on === "number" ? output.grounded_on : 0;
  if (!answer) return null;
  return (
    <div className={styles.resultStack}>
      <p className={styles.briefText}>{answer}</p>
      <span className={styles.agentNote}>Grounded on {grounded} fact{grounded === 1 ? "" : "s"}.</span>
    </div>
  );
}
