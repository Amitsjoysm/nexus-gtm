import { useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, ButtonLink, EmptyState, ErrorState, Icons, Select, Skeleton,
} from "@/components/ui";
import { ConversationTimeline } from "@/components/engagement/ConversationTimeline";
import { ResponseTimes } from "@/components/engagement/ResponseTimes";
import { when, whenDay } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useEngagementStatus } from "@/app/EngagementContext";
import { MAIL_PAGE } from "@/lib/api";
import { cn } from "@/lib/cn";
import type {
  DeskQueueItem, DeskScheduledItem, InboxFilters, InboxList, InboxThreadDetail, ReplyCategory,
  RestartSuggestion,
} from "@/lib/types";
import { Queue, ReplyDetail, Scheduled, stepThrough } from "./ReplyDeskPage";
import { WriteAgain } from "./WriteAgain";
import styles from "./MailPage.module.css";

/**
 * Mail: one screen for everything an SDR reads and answers (2026-11-04, asked for as "like
 * Outlook", and "Mail and Replies merged for easy access"). It was two screens: Mail listed every
 * conversation read-only and sent you to Replies to answer, and Replies held the answering.
 *
 * Folders down the side, the list beside them, the conversation on the right:
 *
 * * **Needs reply**: the reply desk's queue. Answer with the AI's suggestion, save to Drafts, or
 *   decide what happens next, without leaving the conversation.
 * * **All mail** and **Sent, no reply**: every conversation on the viewer's mailboxes. Opening one
 *   whose reply is still open shows the same answer box, so nobody has to go elsewhere to reply.
 * * **Scheduled**, **Write again**, **Handled**: the desk's other tabs, as they were.
 *
 * Sending stays where it was: `desk.send_response` (the do-not-contact gate, the exactly-once send,
 * the charge). An inbound message's quoted history is folded under "Show quoted text". Up and Down
 * move through a list, as in Outlook. A rep reads their own mailboxes; a manager can switch to the
 * team's. Old Replies links forward here (`ReplyDeskPage`).
 */

type Folder = "needs_reply" | "all" | "sent" | "scheduled" | "write_again" | "handled";

const FOLDERS: { key: Folder; label: string; icon: ReactNode }[] = [
  { key: "needs_reply", label: "Needs reply", icon: <Icons.InboxIcon /> },
  { key: "all", label: "All mail", icon: <Icons.MailIcon /> },
  { key: "sent", label: "Sent, no reply", icon: <Icons.SendIcon /> },
  { key: "scheduled", label: "Scheduled", icon: <Icons.ClockIcon /> },
  { key: "write_again", label: "Write again", icon: <Icons.RefreshIcon /> },
  { key: "handled", label: "Handled", icon: <Icons.CheckIcon /> },
];

function isFolder(value: string | null): value is Folder {
  return FOLDERS.some((f) => f.key === value);
}

export function MailPage() {
  const api = useApiClient();
  const status = useEngagementStatus();
  const [params, setParams] = useSearchParams();
  const folderParam = params.get("folder");
  const folder: Folder = isFolder(folderParam) ? folderParam : "needs_reply";
  const [team, setTeam] = useState(false);

  // The counts on the folders, read whichever folder is open, so the rail always says where work is.
  const needs = useApi<DeskQueueItem[]>((s) => api.deskQueue("needs_action", team, s), [team]);
  const scheduled = useApi<DeskScheduledItem[]>((s) => api.deskScheduled(team, s), [team]);
  const restart = useApi<RestartSuggestion[]>((s) => api.restartSuggestions(s), []);
  const handled = useApi<DeskQueueItem[]>(
    (s) => (folder === "handled" ? api.deskQueue("handled", team, s) : Promise.resolve([])),
    [folder, team],
  );
  const counts: Partial<Record<Folder, number | undefined>> = {
    needs_reply: needs.data?.length, scheduled: scheduled.data?.length,
    write_again: restart.data?.length,
  };

  function go(next: { folder?: Folder; reply?: string | null; thread?: string | null }) {
    const p = new URLSearchParams(params);
    if (next.folder) {
      p.set("folder", next.folder);
      p.delete("reply");
      p.delete("thread");
    }
    if (next.reply === null) p.delete("reply");
    else if (next.reply) p.set("reply", next.reply);
    if (next.thread === null) p.delete("thread");
    else if (next.thread) p.set("thread", next.thread);
    setParams(p, { replace: true });
  }

  const afterReply = () => { needs.refetch(); scheduled.refetch(); };

  return (
    <div className={styles.page}>
      <PageHeader
        title="Mail"
        description="Every conversation from your mailboxes. Answer replies, decide what happens next, and see who is scheduled. Nothing is sent until you press Send."
        actions={status?.can_manage && (
          <>
            <Button variant="secondary" size="sm" iconLeft={<Icons.UsersIcon />}
              aria-pressed={team} onClick={() => setTeam((t) => !t)}>
              {team ? "Show mine" : "Show team"}
            </Button>
            <ButtonLink to="/settings/replies" size="sm" iconLeft={<Icons.SettingsIcon />}>
              Reply settings
            </ButtonLink>
          </>
        )}
      />

      <div className={styles.shell}>
        <nav className={styles.folders} aria-label="Mail folders">
          <ul className={styles.folderList}>
            {FOLDERS.map((f) => {
              const count = counts[f.key];
              return (
                <li key={f.key}>
                  <button type="button" className={styles.folder}
                    aria-current={f.key === folder ? "page" : undefined}
                    onClick={() => go({ folder: f.key })}>
                    <span className={styles.folderIcon} aria-hidden="true">{f.icon}</span>
                    <span className={styles.folderLabel}>{f.label}</span>
                    {count ? <span className={styles.folderCount}>{count}</span> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className={styles.content}>
          {folder === "needs_reply" && (
            <>
              <ResponseTimes team={team} />
              <Queue
                state={needs} selected={params.get("reply")} onSelect={(id) => go({ reply: id })}
                onBack={() => go({ reply: null })}
                onDone={() => { afterReply(); go({ reply: null }); }}
                canAssign={Boolean(status?.can_manage)}
                emptyTitle="Nothing needs you right now"
                emptyText="When someone replies with interest, a question or a referral, or a reply needs your decision, it waits here."
              />
            </>
          )}
          {(folder === "all" || folder === "sent") && (
            <Threads key={`${folder}:${team}`} folder={folder} team={team}
              selected={params.get("thread")} onOpen={(id) => go({ thread: id })}
              onReplied={afterReply} />
          )}
          {folder === "scheduled" && <Scheduled state={scheduled} onChanged={scheduled.refetch} />}
          {folder === "write_again" && <WriteAgain state={restart} onChanged={restart.refetch} />}
          {folder === "handled" && (
            <Queue
              state={handled} selected={params.get("reply")} onSelect={(id) => go({ reply: id })}
              onBack={() => go({ reply: null })} onDone={handled.refetch} canAssign={false}
              emptyTitle="Nothing handled yet"
              emptyText="Replies you have closed, and people who declined or unsubscribed, are kept here as the record of why."
            />
          )}
        </div>
      </div>
    </div>
  );
}

/* ---- All mail and Sent ------------------------------------------------------------------------ */

function Threads({ folder, team, selected, onOpen, onReplied }: {
  folder: "all" | "sent";
  team: boolean;
  selected: string | null;
  onOpen: (id: string | null) => void;
  onReplied: () => void;
}) {
  const api = useApiClient();
  const [filters, setFilters] = useState<InboxFilters>({});
  const query: InboxFilters = { ...filters, team, unanswered: folder === "sent" || undefined };
  const list = useApi<InboxList>((s) => api.inboxThreads(query, s), [JSON.stringify(query)]);
  const threads = list.data?.threads ?? [];
  const items = useMemo(() => threads.map((t) => ({ id: t.thread_id })), [threads]);
  const filtered = Boolean(filters.mailbox_id || filters.campaign_id || filters.replied || filters.unread);
  const set = (patch: Partial<InboxFilters>) => setFilters((f) => ({ ...f, ...patch }));

  return (
    <>
      <div className={styles.filters} role="group" aria-label="Filter conversations">
        <Select
          aria-label="Mailbox" value={filters.mailbox_id ?? ""}
          onChange={(e) => set({ mailbox_id: e.target.value || undefined })}
          options={[{ value: "", label: "All mailboxes" },
            ...(list.data?.mailboxes ?? []).map((m) => ({ value: m.id, label: m.label }))]}
        />
        <Select
          aria-label="Campaign" value={filters.campaign_id ?? ""}
          onChange={(e) => set({ campaign_id: e.target.value || undefined })}
          options={[{ value: "", label: "All campaigns" },
            ...(list.data?.campaigns ?? []).map((c) => ({ value: c.id, label: c.label }))]}
        />
        {folder === "all" && (
          <>
            <button type="button" className={styles.chip} aria-pressed={Boolean(filters.replied)}
              onClick={() => set({ replied: !filters.replied })}>
              Replied
            </button>
            <button type="button" className={styles.chip} aria-pressed={Boolean(filters.unread)}
              onClick={() => set({ unread: !filters.unread })}
              title="A reply nobody has answered or decided yet">
              Waiting on you
            </button>
          </>
        )}
        {filtered && (
          <button type="button" className={styles.clear} onClick={() => setFilters({})}>
            Clear filters
          </button>
        )}
      </div>

      {list.error ? (
        <ErrorState title="Couldn't load your mail" message={list.error.detail} onRetry={list.refetch} />
      ) : !list.data ? (
        <div className={styles.grid}>
          <div className={styles.listPane}>
            {[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} width="100%" height={72} />)}
          </div>
          <Skeleton width="100%" height={420} />
        </div>
      ) : threads.length === 0 ? (
        <EmptyState
          icon={folder === "sent" ? <Icons.SendIcon /> : <Icons.MailIcon />}
          title={filtered ? "No conversations match" : folder === "sent" ? "Everyone you wrote to has replied" : "No emails yet"}
          description={filtered
            ? "Try another mailbox or campaign, or clear the filters."
            : folder === "sent"
              ? "Conversations nothing has come back on yet are listed here."
              : "Emails sent from your campaigns and the composer, and every reply to them, appear here."}
          action={filtered
            ? <Button variant="secondary" onClick={() => setFilters({})}>Clear filters</Button>
            : folder === "all"
              ? <ButtonLink to="/engagement/campaigns" iconLeft={<Icons.SendIcon />}>Go to campaigns</ButtonLink>
              : undefined}
        />
      ) : (
        <div className={styles.grid} data-open={selected ? "detail" : "list"}>
          <div className={styles.listPane}>
            <ul className={styles.list} aria-label="Conversations">
              {threads.map((t) => {
                const name = t.contact_name || t.contact_email || "Unknown contact";
                return (
                  <li key={t.thread_id}>
                    <button type="button" className={styles.item}
                      aria-current={t.thread_id === selected ? "true" : undefined}
                      onClick={() => onOpen(t.thread_id)}
                      onKeyDown={(e) => stepThrough(e, items, t.thread_id, onOpen)}>
                      <span className={styles.itemHead}>
                        {t.unread && <span className={styles.dot} aria-hidden="true" />}
                        <span className={t.unread ? `${styles.from} ${styles.strong}` : styles.from}>{name}</span>
                        {t.message_count > 1 && <span className={styles.count}>{t.message_count}</span>}
                        <time className={styles.time} dateTime={t.last_at ?? undefined}>{whenDay(t.last_at)}</time>
                      </span>
                      <span className={t.unread ? `${styles.subject} ${styles.strong}` : styles.subject}>{t.subject}</span>
                      <span className={styles.snippet}>
                        {t.last_direction === "out" && <span className={styles.you}>You: </span>}
                        {t.snippet}
                      </span>
                      <span className={styles.meta}>
                        {t.unread && <span className={styles.srOnly}>Waiting on you. </span>}
                        {t.campaign_name || "One-off email"}
                        {list.data?.team && <> · {t.mailbox_email}</>}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            {threads.length >= MAIL_PAGE && (
              <p className={styles.more}>The newest {MAIL_PAGE} conversations. Filter to find older ones.</p>
            )}
          </div>
          <div className={styles.readingPane}>
            {selected ? (
              <>
                <button type="button" className={styles.back} onClick={() => onOpen(null)}>
                  <Icons.ChevronLeftIcon aria-hidden /> All conversations
                </button>
                <Reading key={selected} threadId={selected}
                  onReplied={() => { onReplied(); list.refetch(); }} />
              </>
            ) : (
              <div className={styles.placeholder}>
                <Icons.MailIcon aria-hidden />
                <p>Choose a conversation to read it. Up and Down move through the list.</p>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}

/**
 * One conversation. When its newest reply is still open, the reply desk's own view is shown
 * instead, with the answer box and the decisions, so a reply is answered where it is read.
 */
function Reading({ threadId, onReplied }: { threadId: string; onReplied: () => void }) {
  const api = useApiClient();
  const view = useApi<InboxThreadDetail>((s) => api.inboxThread(threadId, s), [threadId]);
  const messages = useMemo(() => (view.data?.messages ?? []).map((m) => ({
    id: m.id, direction: m.direction, subject: m.subject, body: m.body, at: m.at,
    quoted: m.quoted,
    who: m.direction === "in" ? view.data?.thread.contact_name || undefined : undefined,
    context: m.direction === "out" && m.from_addr ? `From ${m.from_addr}` : undefined,
    category: (m.category as ReplyCategory | null) ?? null,
  })), [view.data]);

  if (view.error) {
    return <ErrorState title="Couldn't open this conversation" message={view.error.detail} onRetry={view.refetch} />;
  }
  if (!view.data) return <Skeleton width="100%" height={420} />;
  const { thread, reply_id: replyId, reply_open: replyOpen } = view.data;
  if (replyId && replyOpen) {
    return <ReplyDetail key={replyId} id={replyId} canAssign={false}
      onDone={() => { onReplied(); view.refetch(); }} />;
  }
  const bounced = view.data.messages.some((m) => m.status === "bounced");
  return (
    <article className={styles.reading} aria-labelledby="mail-subject">
      <header className={styles.readingHead}>
        <h2 id="mail-subject" className={styles.readingSubject}>{thread.subject}</h2>
        <p className={styles.readingWho}>
          <span className={styles.strong}>{thread.contact_name || thread.contact_email}</span>
          {thread.contact_email && thread.contact_name && <span className={styles.mutedInline}> {thread.contact_email}</span>}
          {thread.account_id && (
            <> · <Link to={`/accounts/${thread.account_id}`} className={styles.link}>{thread.account_name || "Account"}</Link></>
          )}
        </p>
        <p className={styles.readingMeta}>
          {thread.campaign_name || "One-off email"} · {thread.mailbox_email} · last {when(thread.last_at)}
          {bounced && <> <Badge tone="danger" dot>Bounced</Badge></>}
        </p>
        <p className={cn(styles.mutedInline, styles.readingNote)}>
          {replyId
            ? <>Their reply was handled. <Link className={styles.link} to={`/engagement/mail?folder=handled&reply=${replyId}`}>See how</Link></>
            : "No reply yet. The campaign sends its follow-ups on its own."}
        </p>
      </header>
      <ConversationTimeline messages={messages} />
    </article>
  );
}

export default MailPage;
