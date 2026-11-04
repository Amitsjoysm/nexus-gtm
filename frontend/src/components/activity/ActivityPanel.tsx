import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Badge, ErrorState, Icons, Skeleton } from "@/components/ui";
import { CATEGORY, when } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { HistoryItem, ReplyCategory } from "@/lib/types";
import styles from "./ActivityPanel.module.css";

/**
 * What has already been said to a person, or to everyone at an account: every call logged and every
 * email sent or received, newest first (SDR round 2, 5.2). The question a rep asks before every
 * touch, answered in one list instead of a call console per contact and an Emails tab per account.
 *
 * Mount it with one line: `<ActivityPanel contactId={id} />` or `<ActivityPanel accountId={id} />`.
 * It fetches its own rows and owns its loading, empty and error states. An account's history names
 * the person on each entry; a contact's does not repeat them.
 */

export interface ActivityPanelProps {
  contactId?: string;
  accountId?: string;
  /** Tighter spacing for a row expanded in place (the Inbox). */
  compact?: boolean;
}

type Show = "all" | "call" | "email";

const SHOW: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "call", label: "Calls" },
  { value: "email", label: "Emails" },
];

/** 95 → "1m 35s"; a call with no measured length says nothing rather than "0s". */
export function duration(seconds: number | null): string {
  if (seconds == null || seconds <= 0) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${s.toString().padStart(2, "0")}s` : `${s}s`;
}

export function ActivityPanel({ contactId, accountId, compact = false }: ActivityPanelProps) {
  const api = useApiClient();
  const [show, setShow] = useState<Show>("all");
  const history = useApi<HistoryItem[]>(
    (s) => (contactId ? api.contactActivity(contactId, s)
      : accountId ? api.accountActivity(accountId, s) : Promise.resolve([])),
    [contactId, accountId],
  );
  const rows = useMemo(
    () => (history.data ?? []).filter((i) => show === "all" || i.kind === show),
    [history.data, show],
  );
  const counts = useMemo(() => {
    const all = history.data ?? [];
    return { all: all.length, call: all.filter((i) => i.kind === "call").length,
             email: all.filter((i) => i.kind === "email").length };
  }, [history.data]);
  const named = Boolean(accountId) && !contactId;

  if (history.error) {
    return (
      <ErrorState title="Couldn't load the history" message={history.error.detail}
        onRetry={history.refetch} />
    );
  }
  return (
    <section className={compact ? `${styles.panel} ${styles.compact}` : styles.panel}
      aria-label="Call and email history">
      <div className={styles.head}>
        <h3 className={styles.title}>History</h3>
        {history.data && counts.all > 0 && (
          <div className={styles.show} role="group" aria-label="Show">
            {SHOW.map((s) => (
              <button key={s.value} type="button" className={styles.showButton}
                aria-pressed={show === s.value} onClick={() => setShow(s.value)}>
                {s.label} <span className={styles.count}>{counts[s.value]}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {!history.data ? (
        <div className={styles.loading} aria-busy="true">
          {[0, 1, 2].map((i) => <Skeleton key={i} width="100%" height={compact ? 48 : 60} />)}
        </div>
      ) : counts.all === 0 ? (
        <p className={styles.empty}>
          No calls or emails yet. Calls logged and emails sent from InfoJoy GTM appear here.
        </p>
      ) : rows.length === 0 ? (
        <p className={styles.empty}>{show === "call" ? "No calls logged yet." : "No emails yet."}</p>
      ) : (
        <ol className={styles.list}>
          {rows.map((item) => (
            <li key={`${item.kind}-${item.id}`} className={styles.item}>
              {item.kind === "call" ? <CallEntry item={item} named={named} />
                : <EmailEntry item={item} named={named} />}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function CallEntry({ item, named }: { item: HistoryItem; named: boolean }) {
  const length = duration(item.duration_s);
  const meta = [item.by && `by ${item.by}`, length, item.placed_live && "placed in the app"]
    .filter(Boolean).join(" · ");
  return (
    <>
      <span className={`${styles.icon} ${styles.callIcon}`} aria-hidden="true"><Icons.PhoneIcon /></span>
      <div className={styles.body}>
        <p className={styles.line}>
          <span className={styles.what}>
            {named && item.contact_name ? `Call with ${item.contact_name}` : "Call"}
          </span>
          <Badge tone="neutral">{item.disposition || "Logged"}</Badge>
          {item.category && <span className={styles.muted}>{item.category}</span>}
          <time className={styles.time} dateTime={item.at ?? undefined}>{when(item.at)}</time>
        </p>
        {meta && <p className={styles.meta}>{meta}</p>}
        {item.notes && <p className={styles.text}>{item.notes}</p>}
      </div>
    </>
  );
}

function EmailEntry({ item, named }: { item: HistoryItem; named: boolean }) {
  const inbound = item.direction === "in";
  const category = item.reply_category ? CATEGORY[item.reply_category as ReplyCategory] : null;
  const who = inbound
    ? `${named && item.contact_name ? item.contact_name : "They"} replied`
    : named && item.contact_name ? `Email to ${item.contact_name}` : "Email sent";
  const meta = [item.campaign_name || "One-off email", item.mailbox && (inbound ? `to ${item.mailbox}` : `from ${item.mailbox}`)]
    .filter(Boolean).join(" · ");
  return (
    <>
      <span className={`${styles.icon} ${inbound ? styles.inIcon : styles.outIcon}`} aria-hidden="true">
        {inbound ? <Icons.MessageIcon /> : <Icons.SendIcon />}
      </span>
      <div className={styles.body}>
        <p className={styles.line}>
          <span className={styles.what}>{who}</span>
          {item.status === "bounced" && <Badge tone="danger" dot>Bounced</Badge>}
          {category && <Badge tone={category.tone}>{category.label}</Badge>}
          <time className={styles.time} dateTime={item.at ?? undefined}>{when(item.at)}</time>
        </p>
        {item.subject && <p className={styles.subject}>{item.subject}</p>}
        {item.preview && <p className={`${styles.text} ${styles.clamped}`}>{item.preview}</p>}
        <p className={styles.meta}>
          {meta}
          {item.thread_link && (
            <> · <Link to={item.thread_link} className={styles.link}>Open conversation</Link></>
          )}
        </p>
      </div>
    </>
  );
}
