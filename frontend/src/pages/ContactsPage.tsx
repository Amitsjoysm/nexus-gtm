import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  Spinner,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { CallConsole } from "@/components/CallConsole";
import { EmailComposer } from "@/components/EmailComposer";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { strengthMeta } from "@/lib/display";
import { timeAgo } from "@/lib/format";
import type { CallTask, ContactLookalike, WorkspaceContact } from "@/lib/types";
import styles from "./ContactsPage.module.css";

const ALL = "__all__";

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  valid: "success",
  risky: "warning",
  unknown: "neutral",
  invalid: "danger",
};

// Mirrors NEXUS_EMAIL_REVERIFY_COOLDOWN_DAYS: a confirmed-valid address is re-checkable only
// after this window (the backend enforces it with a 429; this just saves the click).
const REVERIFY_COOLDOWN_DAYS = 30;
const verifiedFresh = (c: WorkspaceContact): boolean =>
  c.email_status === "valid" &&
  !!c.email_checked_at &&
  Date.now() - Date.parse(c.email_checked_at) < REVERIFY_COOLDOWN_DAYS * 86_400_000;
const reverifyDueDate = (c: WorkspaceContact): string =>
  new Date(
    Date.parse(c.email_checked_at as string) + REVERIFY_COOLDOWN_DAYS * 86_400_000,
  ).toLocaleDateString();

export function ContactsPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const toast = useToast();
  const contacts = useApi<WorkspaceContact[]>((signal) => api.listWorkspaceContacts(undefined, signal), []);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState(ALL);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reverifying, setReverifying] = useState(false);
  const [callTask, setCallTask] = useState<CallTask | null>(null);
  const [emailFor, setEmailFor] = useState<WorkspaceContact | null>(null);
  const [callingId, setCallingId] = useState<string | null>(null);
  // "Similar people" — opens a modal ranking the workspace's other contacts against this one.
  const [similarFor, setSimilarFor] = useState<WorkspaceContact | null>(null);
  const [similarLoading, setSimilarLoading] = useState(false);
  const [similarData, setSimilarData] = useState<ContactLookalike[] | null>(null);
  const [similarId, setSimilarId] = useState<string | null>(null);

  async function findSimilar(c: WorkspaceContact) {
    setSimilarId(c.id);
    setSimilarFor(c);
    setSimilarData(null);
    setSimilarLoading(true);
    try {
      const res = await api.findContactLookalikes(c.id, 10);
      setSimilarData(res.lookalikes);
    } catch (err) {
      setSimilarFor(null);
      toast.error("Couldn't find similar people", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setSimilarLoading(false);
      setSimilarId(null);
    }
  }

  async function startCall(c: WorkspaceContact) {
    setCallingId(c.id);
    try {
      setCallTask(
        await api.createCallTask({
          account_id: c.account_id,
          contact_id: c.id,
          reason: `Outbound call · ${c.account_name}`,
        }),
      );
    } catch (err) {
      toast.error("Couldn't start call", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setCallingId(null);
    }
  }

  const rows = contacts.data ?? [];
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((c) => {
      if (q && ![c.full_name, c.title, c.email, c.account_name].some((v) => (v ?? "").toLowerCase().includes(q)))
        return false;
      if (statusFilter !== ALL && (c.email_status ?? "none") !== statusFilter) return false;
      return true;
    });
  }, [rows, query, statusFilter]);

  async function reverifyAll() {
    setReverifying(true);
    try {
      const res = await api.reverifyContacts(true);
      const verified = res.statuses.valid ?? 0;
      toast.success(
        "Re-verification complete",
        `Checked ${res.checked}, updated ${res.updated}${verified ? ` · ${verified} valid` : ""}.`,
      );
      contacts.refetch();
    } catch (err) {
      toast.error("Re-verify failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setReverifying(false);
    }
  }

  const unverifiedCount = useMemo(
    () => rows.filter((c) => c.email && (!c.email_status || c.email_status === "unknown")).length,
    [rows],
  );

  async function verify(c: WorkspaceContact) {
    setBusyId(c.id);
    try {
      await api.enrichContact(c.id);
      toast.success("Verifying…", `Re-checked ${c.full_name}'s email.`);
      contacts.refetch();
    } catch (err) {
      toast.error("Verify failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  const columns: Column<WorkspaceContact>[] = useMemo(
    () => [
      {
        key: "full_name",
        header: "Name",
        sortValue: (c) => c.full_name,
        render: (c) => <span className={styles.name}>{c.full_name}</span>,
      },
      {
        key: "title",
        header: "Title",
        sortValue: (c) => c.title,
        render: (c) => c.title ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "account_name",
        header: "Account",
        sortValue: (c) => c.account_name,
        render: (c) => <span className={styles.account}>{c.account_name}</span>,
      },
      {
        key: "email",
        header: "Email",
        hideOnMobile: true,
        sortValue: (c) => c.email,
        render: (c) =>
          c.email ? <span className={styles.mono}>{c.email}</span> : <span className={styles.muted}>—</span>,
      },
      {
        key: "phone",
        header: "Phone",
        hideOnMobile: true,
        sortValue: (c) => c.phone,
        render: (c) =>
          c.phone ? (
            <a
              href={`tel:${c.phone.replace(/[^\d+]/g, "")}`}
              className={styles.mono}
              onClick={(e) => e.stopPropagation()}
            >
              {c.phone}
            </a>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "email_status",
        header: "Status",
        sortValue: (c) => c.email_status,
        render: (c) =>
          c.email_status ? (
            <Badge tone={STATUS_TONE[c.email_status] ?? "neutral"} dot>
              {c.email_status}
            </Badge>
          ) : c.email ? (
            // Has an address but no verdict yet — say so (and offer "Verify"), don't show a blank.
            <Badge tone="neutral" dot>
              unverified
            </Badge>
          ) : (
            <span className={styles.muted}>no email</span>
          ),
      },
      {
        key: "email_checked_at",
        header: "Checked",
        hideOnMobile: true,
        sortValue: (c) => c.email_checked_at,
        render: (c) =>
          c.email_checked_at ? (
            <span className={styles.muted} title={new Date(c.email_checked_at).toLocaleString()}>
              {timeAgo(c.email_checked_at)}
            </span>
          ) : (
            <span className={styles.muted}>never</span>
          ),
      },
      {
        key: "linkedin_url",
        header: "LinkedIn",
        hideOnMobile: true,
        render: (c) =>
          c.linkedin_url ? (
            <a
              href={c.linkedin_url}
              target="_blank"
              rel="noreferrer noopener"
              className={styles.account}
              onClick={(e) => e.stopPropagation()}
            >
              View
            </a>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        render: (c) => (
          <span className={styles.rowActions} onClick={(e) => e.stopPropagation()}>
            <Button
              size="sm"
              variant="ghost"
              loading={busyId === c.id}
              disabled={verifiedFresh(c)}
              title={
                verifiedFresh(c)
                  ? `Verified valid ${timeAgo(c.email_checked_at as string)} — re-verification opens ${reverifyDueDate(c)}`
                  : undefined
              }
              onClick={() => verify(c)}
              aria-label={`Verify ${c.full_name}'s email`}
            >
              Verify
            </Button>
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.UsersIcon />}
              loading={similarId === c.id}
              onClick={() => findSimilar(c)}
              aria-label={`Find people similar to ${c.full_name}`}
            >
              Similar
            </Button>
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.MessageIcon />}
              onClick={() => setEmailFor(c)}
              aria-label={`Draft an email to ${c.full_name}`}
            >
              Email
            </Button>
            <Button
              size="sm"
              variant="secondary"
              iconLeft={<Icons.PhoneIcon />}
              loading={callingId === c.id}
              onClick={() => startCall(c)}
              aria-label={`Call ${c.full_name}`}
            >
              Call
            </Button>
          </span>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busyId, callingId, similarId],
  );

  return (
    <div>
      <PageHeader
        title="Contacts"
        description="Every person across this workspace. Click a row to open their account."
      />

      <div className={styles.toolbar}>
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search name, title, email, or account…"
          iconLeft={<Icons.SearchIcon />}
          aria-label="Search contacts"
        />
        <Select
          aria-label="Email status"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          options={[
            { value: ALL, label: "Any status" },
            { value: "valid", label: "Valid" },
            { value: "risky", label: "Risky" },
            { value: "unknown", label: "Unknown" },
            { value: "invalid", label: "Invalid" },
            { value: "none", label: "Unverified" },
          ]}
        />
        <span className={styles.count}>
          {visible.length} of {rows.length}
        </span>
        <Button
          size="sm"
          variant="secondary"
          loading={reverifying}
          disabled={unverifiedCount === 0}
          onClick={reverifyAll}
          title={
            unverifiedCount === 0
              ? "Every contact with an email already has a verdict"
              : `Re-check ${unverifiedCount} address${unverifiedCount === 1 ? "" : "es"} against the verifier`
          }
        >
          Re-verify {unverifiedCount > 0 ? `(${unverifiedCount})` : "emails"}
        </Button>
      </div>

      {contacts.error && !contacts.data ? (
        <ErrorState title="Couldn't load contacts" message={contacts.error.detail} onRetry={contacts.refetch} />
      ) : (
        <DataTable<WorkspaceContact>
          columns={columns}
          rows={visible}
          getRowKey={(c) => c.id}
          loading={contacts.loading && !contacts.data}
          skeletonRows={6}
          onRowClick={(c) => navigate(`/accounts/${c.account_id}`)}
          caption="Workspace contacts"
          empty={
            <EmptyState
              icon={<Icons.UsersIcon />}
              title="No contacts yet"
              description="Run a contact discovery (Orchestrator → find contacts) or 'Find contacts' on an account to source real people."
            />
          }
        />
      )}

      {contacts.loading && !contacts.data && <Skeleton width="100%" height={48} />}

      <Modal
        open={!!callTask}
        onClose={() => setCallTask(null)}
        title="Call"
        description="AI script, click-to-dial, and one-tap outcome logging."
      >
        {callTask && (
          <CallConsole task={callTask} autoGenerate onLogged={() => setCallTask(null)} />
        )}
      </Modal>

      <Modal
        open={!!emailFor}
        onClose={() => setEmailFor(null)}
        title={emailFor ? `Email ${emailFor.full_name}` : "Email"}
        description="Hyper-personalized to this contact and account — edit, copy, or open in your mail client."
      >
        {emailFor && (
          <EmailComposer
            accountId={emailFor.account_id}
            contactId={emailFor.id}
            contactName={emailFor.full_name}
            contactEmail={emailFor.email}
          />
        )}
      </Modal>

      <Modal
        open={!!similarFor}
        onClose={() => setSimilarFor(null)}
        title={similarFor ? `People like ${similarFor.full_name}` : "Similar people"}
        description="Ranked by role, seniority, function, and how similar their company is."
      >
        {similarLoading ? (
          <div className={styles.similarLoading}>
            <Spinner size={16} /> Finding similar people…
          </div>
        ) : similarData && similarData.length > 0 ? (
          <ul className={styles.similarList}>
            {similarData.map((p) => {
              const meta = strengthMeta(p.score / 100);
              return (
                <li key={p.contact_id} className={styles.similarItem}>
                  <button
                    type="button"
                    className={styles.similarRow}
                    onClick={() => {
                      setSimilarFor(null);
                      navigate(`/accounts/${p.account_id}`);
                    }}
                  >
                    <span className={styles.similarMain}>
                      <span className={styles.name}>{p.full_name}</span>
                      <span className={styles.muted}>
                        {[p.title, p.account_name].filter(Boolean).join(" · ") || "—"}
                      </span>
                      {p.reasons.length > 0 && (
                        <span className={styles.similarWhy}>{p.reasons.join(" · ")}</span>
                      )}
                    </span>
                    <Badge tone={meta.tone} dot>
                      Match {p.score}
                    </Badge>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState
            compact
            icon={<Icons.UsersIcon />}
            title="No similar people yet"
            description="Add more contacts to your workspace to compare against."
          />
        )}
      </Modal>
    </div>
  );
}

export default ContactsPage;
