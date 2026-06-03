import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Icons,
  Skeleton,
  useToast,
} from "@/components/ui";
import { ChatComposer, ChatThread, useChatSession } from "@/components/chat";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import type { Account, ChatSession, Role } from "@/lib/types";
import styles from "./ChatPage.module.css";

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

/**
 * Orchestrator chat — the flagship discovery surface. A session rail on the left, the active
 * conversation (thread + composer) on the right. `/orchestrator` shows a blank "new conversation"
 * center that creates the session on first send (so we never leave empty sessions lying around);
 * `/orchestrator/:sessionId` drives a live session via `useChatSession`.
 */
export function ChatPage() {
  const { sessionId } = useParams<{ sessionId?: string }>();
  const { session: authSession, tenantEpoch } = useAuth();
  const api = useApiClient();
  const navigate = useNavigate();
  const [railOpen, setRailOpen] = useState(false);

  const sessions = useApi<ChatSession[]>(
    (signal) => api.listChatSessions({}, signal),
    [tenantEpoch],
  );
  const accounts = useApi<Account[]>((signal) => api.listAccounts(signal), [tenantEpoch]);

  const accountNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const account of accounts.data ?? []) map.set(account.id, account.name);
    return map;
  }, [accounts.data]);

  const canManageIcp = authSession ? ROLE_RANK[authSession.role] >= ROLE_RANK.admin : false;

  return (
    <div className={styles.page}>
      <button
        type="button"
        className={styles.railToggle}
        aria-expanded={railOpen}
        aria-controls="chat-rail"
        onClick={() => setRailOpen((open) => !open)}
      >
        <Icons.MenuIcon />
        Conversations
      </button>

      <aside
        id="chat-rail"
        className={styles.rail}
        data-open={railOpen}
        aria-label="Conversations"
      >
        <div className={styles.railHead}>
          <h2 className={styles.railTitle}>Conversations</h2>
          <Button
            size="sm"
            variant="secondary"
            iconLeft={<Icons.PlusIcon />}
            onClick={() => {
              setRailOpen(false);
              navigate("/orchestrator");
            }}
          >
            New
          </Button>
        </div>

        <div className={styles.railBody}>
          <SessionList
            sessions={sessions.data}
            loading={sessions.loading}
            error={sessions.error}
            onRetry={sessions.refetch}
            activeId={sessionId ?? null}
            accountNames={accountNames}
            onPick={() => setRailOpen(false)}
          />
        </div>
      </aside>

      <section className={styles.main}>
        {sessionId ? (
          <Conversation key={sessionId} sessionId={sessionId} canManageIcp={canManageIcp} />
        ) : (
          <BlankState />
        )}
      </section>
    </div>
  );
}

/* ----------------------------- session rail ------------------------------ */

function SessionList({
  sessions,
  loading,
  error,
  onRetry,
  activeId,
  accountNames,
  onPick,
}: {
  sessions: ChatSession[] | null;
  loading: boolean;
  error: ApiError | null;
  onRetry: () => void;
  activeId: string | null;
  accountNames: Map<string, string>;
  onPick: () => void;
}) {
  const groups = useMemo(() => {
    const recent: ChatSession[] = [];
    const byAccount: ChatSession[] = [];
    for (const session of sessions ?? []) {
      (session.account_id ? byAccount : recent).push(session);
    }
    return { recent, byAccount };
  }, [sessions]);

  if (loading) {
    return (
      <div className={styles.skeletonRows}>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className={styles.skeletonRow}>
            <Skeleton width="70%" height={14} />
            <Skeleton width="40%" height={11} />
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <ErrorState
        compact
        title="Couldn't load conversations"
        message={error.detail}
        onRetry={onRetry}
      />
    );
  }

  if (!sessions || sessions.length === 0) {
    return (
      <EmptyState
        compact
        icon={<Icons.SparklesIcon />}
        title="No conversations yet"
        description="Start one to discover and score accounts or contacts."
      />
    );
  }

  return (
    <nav className={styles.groups} aria-label="Your conversations">
      {groups.recent.length > 0 && (
        <SessionGroup label="Recent">
          {groups.recent.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              active={session.id === activeId}
              onPick={onPick}
            />
          ))}
        </SessionGroup>
      )}

      {groups.byAccount.length > 0 && (
        <SessionGroup label="By account">
          {groups.byAccount.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              active={session.id === activeId}
              accountName={session.account_id ? accountNames.get(session.account_id) : undefined}
              onPick={onPick}
            />
          ))}
        </SessionGroup>
      )}
    </nav>
  );
}

function SessionGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.group}>
      <p className={styles.groupHead}>{label}</p>
      <ul className={styles.sessionList}>{children}</ul>
    </div>
  );
}

function SessionRow({
  session,
  active,
  accountName,
  onPick,
}: {
  session: ChatSession;
  active: boolean;
  accountName?: string;
  onPick: () => void;
}) {
  return (
    <li>
      <Link
        to={`/orchestrator/${session.id}`}
        className={styles.sessionRow}
        data-active={active}
        aria-current={active ? "page" : undefined}
        onClick={onPick}
      >
        <span className={styles.sessionTitle}>{session.title || "New conversation"}</span>
        <span className={styles.sessionMeta}>
          {accountName && <span className={styles.sessionAccount}>{accountName}</span>}
          <span>{timeAgo(session.created_at)}</span>
          {session.status === "archived" && (
            <Badge tone="neutral" className={styles.sessionBadge}>
              Archived
            </Badge>
          )}
        </span>
      </Link>
    </li>
  );
}

/* ----------------------------- conversation ------------------------------ */

function Conversation({
  sessionId,
  canManageIcp,
}: {
  sessionId: string;
  canManageIcp: boolean;
}) {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const { session, messages, loading, error, sending, streaming, send, reload } =
    useChatSession(sessionId);

  const [draft, setDraft] = useState("");
  const [savingIcp, setSavingIcp] = useState(false);

  const icpReady = useMemo(
    () =>
      !!session &&
      Object.keys(session.icp_state).length > 0 &&
      session.missing_slots.length === 0,
    [session],
  );

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

  async function handleSaveIcp() {
    if (!session) return;
    setSavingIcp(true);
    try {
      await api.saveChatIcp(session.id);
      toast.success("Saved as ICP", "You'll find it in Relevance.");
    } catch (err) {
      toast.error(
        "Couldn't save the ICP",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSavingIcp(false);
    }
  }

  if (loading) {
    return (
      <div className={styles.conversation}>
        <div className={styles.convHead}>
          <Skeleton width={220} height={18} />
        </div>
        <div className={styles.convSkeleton}>
          <Skeleton width="55%" height={44} />
          <Skeleton width="70%" height={64} className={styles.skeletonRight} />
          <Skeleton width="48%" height={44} />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.conversation}>
        <ErrorState
          title="Couldn't load this conversation"
          message={error.detail}
          onRetry={reload}
        />
      </div>
    );
  }

  return (
    <div className={styles.conversation}>
      <header className={styles.convHead}>
        <div className={styles.convHeadText}>
          <h2 className={styles.convTitle}>{session?.title || "New conversation"}</h2>
          {streaming && (
            <span className={styles.live} aria-label="Live updates connected">
              <span className={styles.liveDot} aria-hidden="true" />
              Live
            </span>
          )}
        </div>
        {canManageIcp && icpReady && (
          <Button
            size="sm"
            variant="secondary"
            iconLeft={<Icons.TargetIcon />}
            loading={savingIcp}
            onClick={handleSaveIcp}
          >
            Save as ICP
          </Button>
        )}
      </header>

      <ChatThread
        messages={messages}
        onChip={(text) => setDraft(text)}
        onOpenRun={(runId) => navigate(`/runs/${runId}`)}
      />

      <div className={styles.composer}>
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

/* ------------------------------ blank state ------------------------------ */

const STARTERS = [
  "Series B fintechs in the US that recently hired a VP of Sales",
  "Heads of Data at mid-market healthcare companies",
  "Companies similar to our best customers in retail",
];

/** No active session: create on first send so we never persist empty conversations. */
function BlankState() {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState("");

  async function start(text: string) {
    setCreating(true);
    try {
      const { session } = await api.createChatSession({ message: text });
      navigate(`/orchestrator/${session.id}`);
    } catch (err) {
      toast.error(
        "Couldn't start the conversation",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
      setCreating(false);
    }
  }

  return (
    <div className={styles.conversation}>
      <div className={styles.blank}>
        <EmptyState
          icon={<Icons.SparklesIcon />}
          title="Start a discovery conversation"
          description="Describe the companies or people you want to reach. The orchestrator asks a few questions, then researches, scores, and shortlists your best-fit matches."
        />
        <div className={styles.starters} role="group" aria-label="Example prompts">
          {STARTERS.map((text) => (
            <button
              key={text}
              type="button"
              className={styles.starter}
              disabled={creating}
              onClick={() => setDraft(text)}
            >
              {text}
            </button>
          ))}
        </div>
      </div>

      <div className={styles.composer}>
        <ChatComposer
          value={draft}
          onValueChange={setDraft}
          onSend={start}
          busy={creating}
          placeholder="Describe the companies or people you want to find…"
        />
      </div>
    </div>
  );
}
