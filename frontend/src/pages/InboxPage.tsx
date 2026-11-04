import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, ButtonLink, EmptyState, ErrorState, Icons, Skeleton, useToast,
} from "@/components/ui";
import { ActivityPanel } from "@/components/activity/ActivityPanel";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useEngineOn } from "@/app/EngagementContext";
import { useSignalWindow } from "@/app/SignalWindowContext";
import { ApiError } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import type { Account, InboxTask, TodayItem, WorkspaceContact } from "@/lib/types";
import styles from "./InboxPage.module.css";

/**
 * The Inbox: the one daily task list (SDR round 2, 6.1; owner decision 2026-10-29, "simple for an
 * SDR"). Four sections, Calls, Emails to send or follow up, Replies and Signals, each a compact table
 * with its count in the header. Within a section, today's work first, then what is overdue. A row
 * opens in place to the person, their company and everything already said to them, beside the one
 * action that clears it; nothing navigates away until that action is pressed.
 *
 * Calls, emails and replies come from the engagement engine's `GET /engagement/today`, which already
 * says each item's section, whether it is due today or overdue, and its action. Signals are the
 * signal tasks of `GET /inbox`. With the engine switched off only Signals show. The Today card on the
 * Dashboard summarises this list and links here; this is where the work is done.
 */

type SectionKey = "calls" | "emails" | "replies" | "signals";

interface Row {
  key: string;
  section: SectionKey;
  title: string;
  detail: string;
  due: "today" | "overdue";
  at: string | null;
  who: string;
  company: string;
  contactId: string | null;
  accountId: string | null;
  /** What the who column says when the task is about nobody in particular (a campaign's review). */
  group: string;
  action: { label: string; to: string; external?: boolean };
  signal?: InboxTask;
}

const SECTIONS: { key: SectionKey; title: string; icon: JSX.Element; empty: string }[] = [
  { key: "calls", title: "Calls", icon: <Icons.PhoneIcon />, empty: "No calls due today." },
  {
    key: "emails", title: "Emails to send or follow up", icon: <Icons.MailIcon />,
    empty: "No emails waiting on you.",
  },
  { key: "replies", title: "Replies", icon: <Icons.MessageIcon />, empty: "No replies waiting on you." },
  { key: "signals", title: "Signals", icon: <Icons.SignalIcon />, empty: "No new signals on your accounts." },
];

const COLLAPSED_KEY = "nexus.inbox.collapsed";

function readCollapsed(): SectionKey[] {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeCollapsed(keys: SectionKey[]) {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify(keys));
  } catch {
    /* a private window keeps sections open; nothing else depends on it */
  }
}

function fromToday(item: TodayItem, index: number): Row {
  return {
    key: `${item.kind}-${item.ref_id ?? index}`,
    section: item.section,
    title: item.title,
    detail: item.detail,
    due: item.due,
    at: item.at,
    who: item.contact_name,
    company: item.account_name,
    contactId: item.contact_id,
    accountId: item.account_id,
    group: item.kind === "review" ? "Campaign"
      : `${item.count} ${item.count === 1 ? "person" : "people"}`,
    action: { label: item.action, to: item.link },
  };
}

/** A signal task is overdue once it has waited since before this morning. */
function fromSignal(task: InboxTask): Row {
  const morning = new Date();
  morning.setHours(0, 0, 0, 0);
  const created = task.created_at ? new Date(task.created_at) : null;
  return {
    key: `signal-${task.id}`,
    section: "signals",
    title: task.title,
    detail: task.reason,
    due: created && created < morning ? "overdue" : "today",
    at: task.created_at ?? null,
    who: "",
    company: task.account_name ?? "",
    contactId: null,
    accountId: task.account_id,
    group: "Signal",
    action: task.signal_url
      ? { label: "Open signal", to: task.signal_url, external: true }
      : { label: "Open account", to: task.account_id ? `/accounts/${task.account_id}` : "/signals" },
    signal: task,
  };
}

function dueLabel(row: Row): string {
  if (row.due === "overdue") return `Overdue · ${timeAgo(row.at).replace(" ago", "")}`;
  if (row.at && new Date(row.at).getTime() > Date.now()) {
    return new Date(row.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  return "Today";
}

export function InboxPage() {
  const api = useApiClient();
  const toast = useToast();
  const engineOn = useEngineOn();
  const { windowDays } = useSignalWindow();
  const [view, setView] = useState<"open" | "done">("open");
  const [openRow, setOpenRow] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<SectionKey[]>(readCollapsed);
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const today = useApi<TodayItem[]>(
    (s) => (engineOn === false ? Promise.resolve([]) : api.engagementToday(s)), [engineOn],
  );
  const inbox = useApi<InboxTask[]>(
    (signal) => api.listInbox(view, signal, windowDays),
    [view, windowDays],
  );

  const sections = useMemo(() => {
    const rows: Row[] = [
      ...(today.data ?? []).map(fromToday),
      ...(inbox.data ?? []).map(fromSignal),
    ];
    // Today's work first, then what is overdue; the server's urgency order survives inside each.
    const rank = (r: Row) => (r.due === "today" ? 0 : 1);
    const by: Record<SectionKey, Row[]> = { calls: [], emails: [], replies: [], signals: [] };
    for (const row of rows) by[row.section].push(row);
    for (const key of Object.keys(by) as SectionKey[]) {
      by[key] = by[key].map((r, i) => [r, i] as const)
        .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([r]) => r);
    }
    return by;
  }, [today.data, inbox.data]);

  const shown = SECTIONS.filter((s) => s.key === "signals" || engineOn !== false);
  const total = shown.reduce((n, s) => n + sections[s.key].length, 0);
  const overdue = shown.reduce((n, s) => n + sections[s.key].filter((r) => r.due === "overdue").length, 0);

  function toggleSection(key: SectionKey) {
    setCollapsed((prev) => {
      const next = prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key];
      writeCollapsed(next);
      return next;
    });
  }

  const complete = useCallback(async (task: InboxTask) => {
    const kind = view === "done" ? "reopen" : "complete";
    setPending((p) => ({ ...p, [task.id]: true }));
    try {
      await (kind === "complete" ? api.completeTask(task.id) : api.reopenTask(task.id));
      inbox.setData((prev) => (prev ?? []).filter((t) => t.id !== task.id));
      setOpenRow(null);
      toast.success(kind === "complete" ? "Signal task done" : "Back on your list", task.title);
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setPending((p) => {
        const next = { ...p };
        delete next[task.id];
        return next;
      });
    }
  }, [api, inbox, toast, view]);

  // J/K move between rows, as the list always allowed; Enter or Space opens one (a real button).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement | null;
      if (!el || el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key !== "j" && key !== "k") return;
      const toggles = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-task-row]"));
      if (!toggles.length) return;
      e.preventDefault();
      const at = toggles.indexOf(document.activeElement as HTMLButtonElement);
      const next = key === "j" ? Math.min(at + 1, toggles.length - 1) : Math.max(at - 1, 0);
      toggles[at === -1 ? 0 : next].focus();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const loading = (engineOn !== false && !today.data && !today.error) || (!inbox.data && !inbox.error);

  return (
    <div className={styles.page}>
      <PageHeader
        title="Inbox"
        description="Everything due today, then what is overdue. Open a row to see the person and what has already been said."
        actions={
          <Button variant="secondary" iconLeft={<Icons.RefreshIcon />}
            onClick={() => { today.refetch(); inbox.refetch(); }}>
            Refresh
          </Button>
        }
      />

      <p className={styles.summary} aria-live="polite">
        {loading ? "Loading your day…" : total === 0 ? "Nothing waiting on you."
          : <>
              <strong>{total}</strong> {total === 1 ? "task" : "tasks"}
              {overdue > 0 && <> · <span className={styles.overdueCount}>{overdue} overdue</span></>}
              <span className={styles.hint}> · <kbd>J</kbd>/<kbd>K</kbd> to move</span>
            </>}
      </p>

      {today.error && engineOn !== false && (
        <ErrorState title="Couldn't load calls, emails and replies" message={today.error.detail}
          onRetry={today.refetch} />
      )}

      <div className={styles.sections}>
        {shown.map((section) => {
          const rows = sections[section.key];
          const isCollapsed = collapsed.includes(section.key);
          const late = rows.filter((r) => r.due === "overdue").length;
          const state = section.key === "signals" ? inbox : today;
          const panelId = `inbox-section-${section.key}`;
          return (
            <section key={section.key} className={styles.section} aria-labelledby={`${panelId}-h`}>
              <h2 className={styles.sectionHead} id={`${panelId}-h`}>
                <button type="button" className={styles.sectionToggle} aria-expanded={!isCollapsed}
                  aria-controls={panelId} onClick={() => toggleSection(section.key)}>
                  <Icons.ChevronDownIcon className={isCollapsed ? styles.chevronClosed : styles.chevron} aria-hidden />
                  <span className={styles.sectionIcon} aria-hidden="true">{section.icon}</span>
                  <span className={styles.sectionTitle}>{section.title}</span>
                  <span className={styles.sectionCount}>{state.data ? rows.length : "…"}</span>
                  {late > 0 && <Badge tone="warning">{late} overdue</Badge>}
                </button>
              </h2>
              <div id={panelId} hidden={isCollapsed}>
                {section.key === "signals" && inbox.error ? (
                  <ErrorState title="Couldn't load signal tasks" message={inbox.error.detail}
                    onRetry={inbox.refetch} />
                ) : !state.data ? (
                  <div className={styles.loading}>
                    <Skeleton width="100%" height={40} />
                    <Skeleton width="100%" height={40} />
                  </div>
                ) : rows.length === 0 ? (
                  <p className={styles.empty}>
                    {section.key === "signals" && view === "done" ? "No completed signal tasks yet." : section.empty}
                  </p>
                ) : (
                  <table className={styles.table}>
                    <caption className={styles.srOnly}>{section.title}</caption>
                    <thead className={styles.srOnly}>
                      <tr>
                        <th scope="col">Who</th>
                        <th scope="col">Task</th>
                        <th scope="col">Due</th>
                        <th scope="col">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <TaskRow key={row.key} row={row} open={openRow === row.key}
                          onToggle={() => setOpenRow((o) => (o === row.key ? null : row.key))}
                          completing={row.signal ? Boolean(pending[row.signal.id]) : false}
                          completeLabel={view === "done" ? "Mark incomplete" : "Done"}
                          onComplete={row.signal ? () => complete(row.signal!) : undefined} />
                      ))}
                    </tbody>
                  </table>
                )}
                {section.key === "signals" && (
                  <button type="button" className={styles.viewSwitch}
                    onClick={() => { setView((v) => (v === "open" ? "done" : "open")); setOpenRow(null); }}>
                    {view === "open" ? "Show completed signal tasks" : "Back to open signal tasks"}
                  </button>
                )}
              </div>
            </section>
          );
        })}
      </div>

      {!loading && total === 0 && engineOn === false && !inbox.error && view === "open" && (
        <EmptyState icon={<Icons.InboxIcon />} title="You're at inbox zero"
          description="Tasks appear when buying signals land on your accounts."
          action={<ButtonLink to="/accounts" iconLeft={<Icons.BuildingIcon />}>Browse accounts</ButtonLink>} />
      )}
    </div>
  );
}

function TaskRow({
  row, open, onToggle, completing, completeLabel, onComplete,
}: {
  row: Row;
  open: boolean;
  onToggle: () => void;
  completing: boolean;
  completeLabel: string;
  onComplete?: () => void;
}) {
  const detailId = `task-${row.key}`;
  const name = row.who || row.company || row.group;
  // With a person named beside it, the title ("Call Jane at Acme") only repeats the who column, so
  // the reason is the task; otherwise the title says what it is.
  const lead = row.who ? row.detail : row.title;
  const sub = row.who ? "" : row.detail;
  return (
    <>
      <tr className={open ? `${styles.row} ${styles.rowOpen}` : styles.row}>
        <td className={styles.whoCell}>
          <button type="button" className={styles.rowToggle} data-task-row aria-expanded={open}
            aria-controls={detailId} onClick={onToggle}>
            <Icons.ChevronRightIcon className={open ? styles.rowChevronOpen : styles.rowChevron} aria-hidden />
            <span className={styles.who}>
              <span className={styles.name}>{name}</span>
              {row.who && row.company && <span className={styles.company}>{row.company}</span>}
            </span>
          </button>
        </td>
        <td className={styles.taskCell} title={row.who ? row.title : undefined}>
          <span className={styles.taskTitle}>{lead}</span>
          {sub && <span className={styles.taskDetail}>{sub}</span>}
        </td>
        <td className={styles.dueCell}>
          <span className={row.due === "overdue" ? styles.overdue : styles.today}>{dueLabel(row)}</span>
        </td>
        <td className={styles.actionCell}>
          <ButtonLink size="sm" variant="secondary" to={row.action.to}
            {...(row.action.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}>
            {row.action.label}
          </ButtonLink>
        </td>
      </tr>
      {open && (
        <tr className={styles.detailRow}>
          <td colSpan={4} id={detailId}>
            <TaskDetail row={row} completing={completing} completeLabel={completeLabel}
              onComplete={onComplete} />
          </td>
        </tr>
      )}
    </>
  );
}

/** The row opened in place: who it is, their company, and everything already said to them. */
function TaskDetail({
  row, completing, completeLabel, onComplete,
}: {
  row: Row;
  completing: boolean;
  completeLabel: string;
  onComplete?: () => void;
}) {
  const api = useApiClient();
  const contact = useApi<WorkspaceContact | null>(
    (s) => (row.contactId ? api.getContact(row.contactId, s) : Promise.resolve(null)), [row.contactId],
  );
  const account = useApi<Account | null>(
    (s) => (!row.contactId && row.accountId ? api.getAccount(row.accountId, s) : Promise.resolve(null)),
    [row.contactId, row.accountId],
  );
  const c = contact.data;
  const companyId = c?.account_id ?? row.accountId;
  const companyName = c?.account_name || account.data?.name || row.company;
  const domain = c?.account_domain ?? account.data?.domain ?? null;

  return (
    <div className={styles.detail}>
      <div className={styles.facts}>
        {row.contactId && (
          <div className={styles.block}>
            <h3 className={styles.blockTitle}>Person</h3>
            {contact.error ? <p className={styles.muted}>Couldn't load this person.</p>
              : !c ? <Skeleton width="80%" height={44} />
              : (
                <dl className={styles.dl}>
                  <dt className={styles.srOnly}>Name</dt>
                  <dd className={styles.factStrong}>{c.full_name}</dd>
                  {c.title && <><dt className={styles.srOnly}>Title</dt><dd>{c.title}</dd></>}
                  {c.email && (
                    <><dt className={styles.srOnly}>Email</dt>
                      <dd><a href={`mailto:${c.email}`} className={styles.factLink}>{c.email}</a></dd></>
                  )}
                  {c.phone && (
                    <><dt className={styles.srOnly}>Phone</dt>
                      <dd><a href={`tel:${c.phone}`} className={styles.factLink}>{c.phone}</a></dd></>
                  )}
                </dl>
              )}
          </div>
        )}
        {companyId && (
          <div className={styles.block}>
            <h3 className={styles.blockTitle}>Company</h3>
            <dl className={styles.dl}>
              <dt className={styles.srOnly}>Name</dt>
              <dd className={styles.factStrong}>
                <Link to={`/accounts/${companyId}`} className={styles.factLink}>{companyName || "Open account"}</Link>
              </dd>
              {domain && <><dt className={styles.srOnly}>Website</dt><dd className={styles.muted}>{domain}</dd></>}
            </dl>
          </div>
        )}
        <div className={styles.block}>
          <h3 className={styles.blockTitle}>Why it's here</h3>
          <p className={styles.why}>{row.title}. {row.detail}</p>
        </div>
        <div className={styles.detailActions}>
          <ButtonLink size="sm" variant="primary" to={row.action.to}
            {...(row.action.external ? { target: "_blank", rel: "noreferrer noopener" } : {})}>
            {row.action.label}
          </ButtonLink>
          {onComplete && (
            <Button size="sm" variant="secondary" loading={completing} onClick={onComplete}
              iconLeft={<Icons.CheckIcon />}>
              {completeLabel}
            </Button>
          )}
        </div>
      </div>
      <div className={styles.history}>
        {row.contactId ? <ActivityPanel contactId={row.contactId} compact />
          : companyId ? <ActivityPanel accountId={companyId} compact />
          : <p className={styles.muted}>This task is about a campaign rather than one person.</p>}
      </div>
    </div>
  );
}

export default InboxPage;
