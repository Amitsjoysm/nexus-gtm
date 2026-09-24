import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, DataTable, EmptyState, ErrorState, Field, Icons, Input, Modal, Select, Skeleton,
  TabPanel, Tabs, Textarea, WorkingIndicator,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { ConversationTimeline } from "@/components/engagement/ConversationTimeline";
import { ResponseTimes } from "@/components/engagement/ResponseTimes";
import { CATEGORY, CATEGORY_OPTIONS, reasonText, when, whenDay } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import type { AsyncState } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useEngagementStatus } from "@/app/EngagementContext";
import { ApiError } from "@/lib/api";
import type {
  DeskDecision, DeskItemDetail, DeskQueueItem, DeskScheduledItem, Member, ReplyCategory,
} from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * The reply desk (spec §9, D22): every reply to a campaign, read and sorted, and nothing sent
 * without a person pressing Send.
 *
 * Needs action holds what a person must answer or decide. Scheduled holds people who asked to hear
 * back on a date or are out of office; their date can be moved or cancelled. Handled is the record
 * of who declined, unsubscribed or was closed, and why.
 */

type TabKey = "needs_action" | "scheduled" | "handled";

function replySubject(subject: string): string {
  const s = subject.trim();
  return /^re:/i.test(s) ? s : `Re: ${s}`;
}

/** The stored suggestion is "subject\n\nbody"; split it back. */
function splitSuggestion(text: string | null, fallbackSubject: string): { subject: string; body: string } {
  if (!text) return { subject: replySubject(fallbackSubject), body: "" };
  const [first, ...rest] = text.split("\n\n");
  return rest.length ? { subject: first, body: rest.join("\n\n") } : { subject: replySubject(fallbackSubject), body: first };
}

export function ReplyDeskPage() {
  const api = useApiClient();
  const status = useEngagementStatus();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabKey) || "needs_action";
  const selected = params.get("reply");
  const [team, setTeam] = useState(false);

  const open = useApi<DeskQueueItem[]>((s) => api.deskQueue("needs_action", team, s), [team]);
  const handled = useApi<DeskQueueItem[]>(
    (s) => (tab === "handled" ? api.deskQueue("handled", team, s) : Promise.resolve([])), [tab, team],
  );
  const scheduled = useApi<DeskScheduledItem[]>((s) => api.deskScheduled(team, s), [team]);

  function go(next: Partial<{ tab: TabKey; reply: string | null }>) {
    const p = new URLSearchParams(params);
    if (next.tab) p.set("tab", next.tab);
    if (next.reply === null) p.delete("reply");
    else if (next.reply) p.set("reply", next.reply);
    setParams(p, { replace: true });
  }

  const tabs = [
    { value: "needs_action", label: "Needs action", count: open.data?.length },
    { value: "scheduled", label: "Scheduled", count: scheduled.data?.length },
    { value: "handled", label: "Handled" },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Replies"
        description="Answers to your campaigns, read and sorted for you. Nothing is sent until you press Send."
        actions={
          <>
            {status?.can_manage && (
              <>
                <Button variant="secondary" size="sm" aria-pressed={team} onClick={() => setTeam((t) => !t)}>
                  {team ? "Show mine" : "Show team"}
                </Button>
                <Link to="/engagement/replies/settings" className={styles.buttonLink}>Reply settings</Link>
              </>
            )}
          </>
        }
      />

      <ResponseTimes team={team} />

      <Tabs items={tabs} value={tab} onChange={(v) => go({ tab: v as TabKey, reply: null })} idPrefix="desk" />

      <TabPanel id="desk-panel-needs_action" active={tab === "needs_action"}>
        <Queue
          state={open} selected={selected} onSelect={(id) => go({ reply: id })}
          onBack={() => go({ reply: null })}
          onDone={() => { open.refetch(); scheduled.refetch(); go({ reply: null }); }}
          canAssign={Boolean(status?.can_manage)}
          emptyTitle="Nothing needs you right now"
          emptyText="When someone replies with interest, a question or a referral, or a reply needs your decision, it waits here."
        />
      </TabPanel>

      <TabPanel id="desk-panel-scheduled" active={tab === "scheduled"}>
        <Scheduled state={scheduled} onChanged={scheduled.refetch} />
      </TabPanel>

      <TabPanel id="desk-panel-handled" active={tab === "handled"}>
        <Queue
          state={handled} selected={selected} onSelect={(id) => go({ reply: id })}
          onBack={() => go({ reply: null })} onDone={handled.refetch} canAssign={false}
          emptyTitle="Nothing handled yet"
          emptyText="Replies you have closed, and people who declined or unsubscribed, are kept here as the record of why."
        />
      </TabPanel>
    </div>
  );
}

/* ---- the queue and one reply --------------------------------------------------------------- */

function Queue({
  state, selected, onSelect, onBack, onDone, canAssign, emptyTitle, emptyText,
}: {
  state: AsyncState<DeskQueueItem[]>;
  selected: string | null;
  onSelect: (id: string) => void;
  onBack: () => void;
  onDone: () => void;
  canAssign: boolean;
  emptyTitle: string;
  emptyText: string;
}) {
  if (state.error) {
    return <ErrorState title="Couldn't load replies" message={state.error.detail} onRetry={state.refetch} />;
  }
  if (!state.data) {
    return (
      <div className={styles.deskGrid}>
        <div className={styles.stack}>
          {[0, 1, 2, 3].map((i) => <Skeleton key={i} width="100%" height={76} />)}
        </div>
        <Skeleton width="100%" height={420} />
      </div>
    );
  }
  if (state.data.length === 0) {
    return <EmptyState icon={<Icons.InboxIcon />} title={emptyTitle} description={emptyText} />;
  }
  const current = selected ?? state.data[0].id;
  return (
    <div className={styles.deskGrid} data-open={selected ? "detail" : "list"}>
      <ul className={styles.deskList} aria-label="Replies">
        {state.data.map((r) => {
          const cat = CATEGORY[(r.corrected_category ?? r.category) as ReplyCategory];
          return (
            <li key={r.id}>
              <button
                type="button"
                className={styles.deskItem}
                aria-current={r.id === current ? "true" : undefined}
                onClick={() => onSelect(r.id)}
              >
                <span className={styles.deskItemHead}>
                  <span className={styles.personName}>{r.contact_name || r.contact_email}</span>
                  <time className={styles.time} dateTime={r.received_at ?? undefined}>{whenDay(r.received_at)}</time>
                </span>
                <span className={styles.muted}>{r.account_name}</span>
                <span className={styles.deskItemFoot}>
                  {cat && <Badge tone={cat.tone}>{cat.label}</Badge>}
                  {r.decision && <Badge tone="neutral">{decisionLabel(r.decision)}</Badge>}
                  {r.responded_at && !r.decision && <Badge tone="neutral">Answered</Badge>}
                </span>
                <span className={styles.preview}>{r.preview}</span>
              </button>
            </li>
          );
        })}
      </ul>
      <div className={styles.deskDetail}>
        <button type="button" className={styles.backButton} onClick={onBack}>
          <Icons.ChevronLeftIcon aria-hidden /> All replies
        </button>
        <ReplyDetail key={current} id={current} onDone={onDone} canAssign={canAssign} />
      </div>
    </div>
  );
}

function decisionLabel(d: DeskDecision): string {
  return { reengage: "Coming back later", block: "Blocked", close: "Closed", meeting: "Meeting booked" }[d];
}

function ReplyDetail({ id, onDone, canAssign }: { id: string; onDone: () => void; canAssign: boolean }) {
  const api = useApiClient();
  const toast = useToast();
  const item = useApi<DeskItemDetail>((s) => api.deskItem(id, s), [id]);
  const members = useApi<Member[]>(
    (s) => (canAssign ? api.memberDirectory(s) : Promise.resolve([])), [canAssign],
  );
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [laterOpen, setLaterOpen] = useState(false);
  const [laterOn, setLaterOn] = useState("");
  const [note, setNote] = useState("");
  const [confirmBlock, setConfirmBlock] = useState(false);

  const d = item.data;
  useEffect(() => {
    if (!d) return;
    const s = splitSuggestion(d.suggested_response, d.subject);
    setSubject(s.subject);
    setBody(s.body);
    setLaterOn(d.resolved_date ?? "");
  }, [d]);

  const conversation = useMemo(() => (d?.conversation ?? []).map((m) => ({
    id: m.id, direction: m.direction, subject: m.subject, body: m.body, at: m.at,
    category: m.id === d?.message_id ? ((d?.corrected_category ?? d?.category) as ReplyCategory) : null,
  })), [d]);

  async function run(label: string, action: () => Promise<unknown>, done?: [string, string?], closes = false) {
    setBusy(label);
    try {
      await action();
      if (done) toast.success(done[0], done[1]);
      if (closes) onDone();
      else item.refetch();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  if (item.error) {
    return <ErrorState title="Couldn't open this reply" message={item.error.detail} onRetry={item.refetch} />;
  }
  if (!d) return <Skeleton width="100%" height={420} />;

  const category = (d.corrected_category ?? d.category) as ReplyCategory;
  const cat = CATEGORY[category];
  const openItem = d.status === "open";

  async function suggest() {
    await run("draft", async () => {
      const fresh = await api.deskDraft(id);
      setSubject(fresh.subject);
      setBody(fresh.body);
      if (fresh.quality_problems.length) {
        toast.error("Check the suggestion", fresh.quality_problems.join("; "));
      }
    });
  }

  async function send() {
    await run("send", async () => {
      const result = await api.deskSend(id, { subject, body });
      if (result.outcome !== "sent") throw new ApiError(409, `Not sent: ${result.reason || result.outcome}.`);
    }, ["Sent", `In the same thread as their reply, from your mailbox.`], category !== "unclear");
  }

  return (
    <article className={styles.detail} aria-labelledby="reply-title">
      <header className={styles.detailHead}>
        <div className={styles.person}>
          <h2 id="reply-title" className={styles.detailTitle}>{d.contact_name || d.contact_email}</h2>
          <span className={styles.muted}>
            {[d.contact_email, d.account_name].filter(Boolean).join(" · ")}
          </span>
        </div>
        <div className={styles.readAs}>
          <span className={styles.muted}>Read as</span>
          <Badge tone={cat.tone}>{cat.label}</Badge>
          {!d.corrected_category && (
            <span className={styles.muted}>{Math.round(d.confidence * 100)}% sure</span>
          )}
        </div>
      </header>

      <div className={styles.correctRow}>
        <Field label="Read it differently?" hint="Your correction is what the reading learns from.">
          <Select
            value={category}
            onChange={(e) => {
              const next = e.target.value as ReplyCategory;
              void run("correct", () => api.deskCorrect(id, next), ["Correction saved"]);
            }}
            options={CATEGORY_OPTIONS}
            disabled={busy !== null}
          />
        </Field>
        {canAssign && (members.data?.length ?? 0) > 0 && (
          <Field label="Assigned to">
            <Select
              value={d.assigned_user_id ?? ""}
              onChange={(e) => void run("assign", () => api.deskAssign(id, e.target.value), ["Reassigned"])}
              options={[
                { value: "", label: "The mailbox owner" },
                ...(members.data ?? []).map((m) => ({ value: m.user_id, label: m.full_name || m.email })),
              ]}
              disabled={busy !== null}
            />
          </Field>
        )}
      </div>

      {d.resolved_date && (
        <p className={styles.notice} role="status">They asked to hear back on {whenDay(d.resolved_date)}.</p>
      )}

      <section aria-label="Conversation" className={styles.conversation}>
        <ConversationTimeline messages={conversation} />
      </section>

      {openItem && (
        <section className={styles.answer} aria-labelledby="answer-title">
          <div className={styles.sectionHead}>
            <h3 id="answer-title" className={styles.sectionTitle}>Your answer</h3>
            <Button size="sm" variant="secondary" iconLeft={<Icons.SparklesIcon />}
              onClick={suggest} disabled={busy !== null}>
              {body ? "Suggest again" : "Suggest a reply"}
            </Button>
          </div>
          {busy === "draft" ? (
            <WorkingIndicator label="Reading the conversation and writing a reply" hint="Usually under 15 seconds." slowAfter={20} />
          ) : (
            <>
              <Field label="Subject">
                <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
              </Field>
              <Field label="Reply" hint="Your signature is added from your mailbox when it sends.">
                <Textarea rows={8} value={body} onChange={(e) => setBody(e.target.value)}
                  placeholder="Write your answer, or ask for a suggestion." />
              </Field>
            </>
          )}
          <div className={styles.formActions}>
            <Button variant="secondary" disabled={busy !== null || !body.trim()} loading={busy === "save"}
              onClick={() => run("save", () => api.deskSaveDraft(id, { subject, body }),
                ["Saved to Drafts", "It is in your mailbox's Drafts folder, in the same thread."])}>
              Save to Drafts
            </Button>
            <Button iconLeft={<Icons.SendIcon />} disabled={busy !== null || !body.trim()} loading={busy === "send"}
              onClick={send}>
              Send
            </Button>
          </div>
        </section>
      )}

      {d.paused_colleagues.length > 0 && (
        <section className={styles.colleagues} aria-labelledby="colleagues-title">
          <h3 id="colleagues-title" className={styles.sectionTitle}>Colleagues paused by this reply</h3>
          <p className={styles.muted}>They stay paused until someone decides. Nothing resumes on its own.</p>
          <ul className={styles.plainList}>
            {d.paused_colleagues.map((c) => (
              <li key={c.enrollment_id} className={styles.colleagueRow}>
                <span>{c.contact_name || "A colleague"}</span>
                {c.actionable ? (
                  <span className={styles.rowActions}>
                    <Button size="sm" variant="ghost" disabled={busy !== null} loading={busy === `resume:${c.enrollment_id}`}
                      onClick={() => run(`resume:${c.enrollment_id}`, () => api.deskColleague(c.enrollment_id, "resume"), ["Resumed"])}>
                      Resume
                    </Button>
                    <Button size="sm" variant="ghost" disabled={busy !== null} loading={busy === `stop:${c.enrollment_id}`}
                      onClick={() => run(`stop:${c.enrollment_id}`, () => api.deskColleague(c.enrollment_id, "stop"), ["Stopped"])}>
                      Stop
                    </Button>
                  </span>
                ) : (
                  <span className={styles.muted}>In a colleague's campaign</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {openItem && (
        <section className={styles.decisions} aria-labelledby="decide-title">
          <h3 id="decide-title" className={styles.sectionTitle}>Decide what happens next</h3>
          <Field label="Note (optional)" hint="Kept with the decision.">
            <Input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} />
          </Field>
          {laterOpen && (
            <div className={styles.laterRow}>
              <Field label="Come back on" hint="Their sequence resumes that morning, in their timezone.">
                <Input type="date" value={laterOn} onChange={(e) => setLaterOn(e.target.value)} />
              </Field>
              <Button disabled={!laterOn || busy !== null} loading={busy === "reengage"}
                onClick={() => run("reengage", () => api.deskDecide(id, { decision: "reengage", reengage_on: laterOn, note }),
                  ["Scheduled", `They come back on ${whenDay(laterOn)}.`], true)}>
                Schedule it
              </Button>
            </div>
          )}
          <div className={styles.decisionButtons}>
            <Button variant="secondary" disabled={busy !== null} loading={busy === "meeting"}
              iconLeft={<Icons.CheckIcon />}
              onClick={() => run("meeting", () => api.deskDecide(id, { decision: "meeting", note }),
                ["Meeting booked", "Recorded on the account; their sequence has stopped."], true)}>
              Meeting booked
            </Button>
            <Button variant="secondary" disabled={busy !== null} aria-expanded={laterOpen}
              onClick={() => setLaterOpen((v) => !v)}>
              Come back later
            </Button>
            <Button variant="ghost" disabled={busy !== null} loading={busy === "close"}
              onClick={() => run("close", () => api.deskDecide(id, { decision: "close", note }), ["Closed"], true)}>
              Close
            </Button>
            <Button variant="ghost" disabled={busy !== null} onClick={() => setConfirmBlock(true)}>
              Do not contact
            </Button>
          </div>
        </section>
      )}

      <Modal
        open={confirmBlock}
        onClose={() => setConfirmBlock(false)}
        title={`Stop contacting ${d.contact_name || d.contact_email}?`}
        description="Their address goes on the do-not-contact list and every sequence they are in stops. A manager can lift it later."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmBlock(false)}>Cancel</Button>
            <Button variant="danger" loading={busy === "block"}
              onClick={() => run("block", async () => {
                await api.deskDecide(id, { decision: "block", note });
                setConfirmBlock(false);
              }, ["Added to do-not-contact"], true)}>
              Do not contact
            </Button>
          </>
        }
      >
        <p className={styles.muted}>{d.contact_email}</p>
      </Modal>
    </article>
  );
}

/* ---- scheduled ------------------------------------------------------------------------------ */

function toLocalInput(iso: string | null): string {
  const d = iso ? new Date(iso) : new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function Scheduled({ state, onChanged }: {
  state: AsyncState<DeskScheduledItem[]>;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [editing, setEditing] = useState<DeskScheduledItem | null>(null);
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  async function run(label: string, action: () => Promise<unknown>, done: string) {
    setBusy(label);
    try {
      await action();
      toast.success(done);
      setEditing(null);
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<DeskScheduledItem>[] = [
    {
      key: "person",
      header: "Person",
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.personName}>{r.contact_name || "Unknown"}</span>
          <span className={styles.muted}>{r.account_name}</span>
        </div>
      ),
    },
    {
      key: "why",
      header: "Why",
      render: (r) => {
        if (r.status_reason === "out_of_office") return "Out of office";
        const why = reasonText(r.status_reason) || "asked to hear back later";
        return why.charAt(0).toUpperCase() + why.slice(1);
      },
    },
    {
      key: "due",
      header: "Back on",
      sortable: true,
      sortValue: (r) => r.due_at ?? "",
      render: (r) => when(r.due_at),
    },
    {
      key: "actions",
      header: <span className={styles.srOnly}>Actions</span>,
      align: "right",
      render: (r) => (
        <div className={styles.rowActions}>
          <Button size="sm" variant="ghost" disabled={busy !== null}
            onClick={() => { setEditing(r); setDate(toLocalInput(r.due_at)); }}>
            Change date
          </Button>
          <Button size="sm" variant="ghost" disabled={busy !== null} loading={busy === `cancel:${r.enrollment_id}`}
            onClick={() => run(`cancel:${r.enrollment_id}`, () => api.cancelScheduled(r.enrollment_id), `Cancelled: ${r.contact_name}`)}>
            Cancel
          </Button>
        </div>
      ),
    },
  ];

  if (state.error) {
    return <ErrorState title="Couldn't load scheduled contacts" message={state.error.detail} onRetry={state.refetch} />;
  }
  return (
    <>
      <DataTable
        columns={columns}
        rows={state.data ?? []}
        getRowKey={(r) => r.enrollment_id}
        loading={!state.data}
        caption="People waiting for a date"
        empty={
          <EmptyState compact icon={<Icons.InboxIcon />} title="Nobody is scheduled"
            description="People who ask to hear back later, or who are out of office, wait here until their date." />
        }
      />
      <Modal
        open={editing !== null}
        onClose={() => setEditing(null)}
        title={`New date for ${editing?.contact_name ?? "this contact"}`}
        description="Their sequence resumes at this time instead."
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
            <Button disabled={!date} loading={editing !== null && busy === `move:${editing.enrollment_id}`}
              onClick={() => editing && run(`move:${editing.enrollment_id}`,
                () => api.rescheduleScheduled(editing.enrollment_id, new Date(date).toISOString()),
                `New date: ${when(new Date(date).toISOString())}`)}>
              Save date
            </Button>
          </>
        }
      >
        <Field label="Resume on" hint="Your local time.">
          <Input type="datetime-local" value={date} onChange={(e) => setDate(e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}

export default ReplyDeskPage;
