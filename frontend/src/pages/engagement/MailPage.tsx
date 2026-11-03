import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, ButtonLink, EmptyState, ErrorState, Icons, Select, Skeleton,
} from "@/components/ui";
import { ConversationTimeline } from "@/components/engagement/ConversationTimeline";
import { when, whenDay } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useEngagementStatus } from "@/app/EngagementContext";
import { MAIL_PAGE } from "@/lib/api";
import type { InboxFilters, InboxList, InboxThreadDetail, ReplyCategory } from "@/lib/types";
import styles from "./MailPage.module.css";

/**
 * Mail: every engagement email sent and received, one conversation per row, with a reading pane
 * (SDR round 2, 6.4, asked for as "a simplified Outlook"). The reply desk shows the replies that
 * need a decision; this shows everything, including what was sent and nobody answered.
 *
 * Read-only on purpose. Answering opens the conversation's reply on the reply desk, which holds the
 * one way an answer is sent (the do-not-contact gate, the exactly-once send, the charge). A rep reads
 * their own mailboxes; a manager can switch to the team's. "Waiting on you" is a reply still open on
 * the desk: there is no read mark, so opening a conversation here does not clear it.
 */
export function MailPage() {
  const api = useApiClient();
  const status = useEngagementStatus();
  const [params, setParams] = useSearchParams();
  const selected = params.get("thread");
  const [filters, setFilters] = useState<InboxFilters>({});

  const list = useApi<InboxList>((s) => api.inboxThreads(filters, s), [JSON.stringify(filters)]);
  const threads = list.data?.threads ?? [];
  const active = selected ?? null;

  function open(threadId: string | null) {
    const next = new URLSearchParams(params);
    if (threadId) next.set("thread", threadId);
    else next.delete("thread");
    setParams(next, { replace: true });
  }

  function set(patch: Partial<InboxFilters>) {
    setFilters((f) => ({ ...f, ...patch }));
  }

  const filtered = Boolean(filters.mailbox_id || filters.campaign_id || filters.replied || filters.unread);

  return (
    <div className={styles.page}>
      <PageHeader
        title="Mail"
        description="Every email you have sent and received, one conversation per row. Answer a reply on the reply desk."
        actions={status?.can_manage && (
          <Button variant="secondary" size="sm" iconLeft={<Icons.UsersIcon />} aria-pressed={Boolean(filters.team)}
            onClick={() => set({ team: !filters.team, mailbox_id: undefined, campaign_id: undefined })}>
            {filters.team ? "Show mine" : "Show team"}
          </Button>
        )}
      />

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
        <button type="button" className={styles.chip} aria-pressed={Boolean(filters.replied)}
          onClick={() => set({ replied: !filters.replied })}>
          Replied
        </button>
        <button type="button" className={styles.chip} aria-pressed={Boolean(filters.unread)}
          onClick={() => set({ unread: !filters.unread })}
          title="A reply nobody has answered or decided yet on the reply desk">
          Waiting on you
        </button>
        {filtered && (
          <button type="button" className={styles.clear}
            onClick={() => setFilters((f) => ({ team: f.team }))}>
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
          icon={<Icons.MailIcon />}
          title={filtered ? "No conversations match" : "No emails yet"}
          description={filtered
            ? "Try another mailbox or campaign, or clear the filters."
            : "Emails sent from your campaigns and the composer, and every reply to them, appear here."}
          action={filtered
            ? <Button variant="secondary" onClick={() => setFilters((f) => ({ team: f.team }))}>Clear filters</Button>
            : <ButtonLink to="/engagement/campaigns" iconLeft={<Icons.SendIcon />}>Go to campaigns</ButtonLink>}
        />
      ) : (
        <div className={styles.grid} data-open={active ? "detail" : "list"}>
          <div className={styles.listPane}>
            <ul className={styles.list} aria-label="Conversations">
              {threads.map((t) => {
                const name = t.contact_name || t.contact_email || "Unknown contact";
                return (
                  <li key={t.thread_id}>
                    <button type="button" className={styles.item}
                      aria-current={t.thread_id === active ? "true" : undefined}
                      onClick={() => open(t.thread_id)}>
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
            {active ? (
              <>
                <button type="button" className={styles.back} onClick={() => open(null)}>
                  <Icons.ChevronLeftIcon aria-hidden /> All conversations
                </button>
                <Reading key={active} threadId={active} />
              </>
            ) : (
              <div className={styles.placeholder}>
                <Icons.MailIcon aria-hidden />
                <p>Choose a conversation to read it.</p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Reading({ threadId }: { threadId: string }) {
  const api = useApiClient();
  const view = useApi<InboxThreadDetail>((s) => api.inboxThread(threadId, s), [threadId]);
  const messages = useMemo(() => (view.data?.messages ?? []).map((m) => ({
    id: m.id, direction: m.direction, subject: m.subject, body: m.body, at: m.at,
    who: m.direction === "in" ? view.data?.thread.contact_name || undefined : undefined,
    context: m.direction === "out" && m.from_addr ? `From ${m.from_addr}` : undefined,
    category: (m.category as ReplyCategory | null) ?? null,
  })), [view.data]);

  if (view.error) {
    return <ErrorState title="Couldn't open this conversation" message={view.error.detail} onRetry={view.refetch} />;
  }
  if (!view.data) return <Skeleton width="100%" height={420} />;
  const { thread, reply_link: replyLink, reply_open: replyOpen } = view.data;
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
        <div className={styles.readingActions}>
          {replyLink ? (
            <ButtonLink to={replyLink} variant={replyOpen ? "primary" : "secondary"} size="sm"
              iconLeft={<Icons.MessageIcon />}>
              {replyOpen ? "Reply" : "Open on the reply desk"}
            </ButtonLink>
          ) : (
            <p className={styles.mutedInline}>No reply yet. The campaign sends its follow-ups on its own.</p>
          )}
        </div>
      </header>
      <ConversationTimeline messages={messages} />
    </article>
  );
}

export default MailPage;
