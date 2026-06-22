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
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { CallConsole } from "@/components/CallConsole";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { CallTask, WorkspaceContact } from "@/lib/types";
import styles from "./ContactsPage.module.css";

const ALL = "__all__";

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  valid: "success",
  risky: "warning",
  unknown: "neutral",
  invalid: "danger",
};

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
  const [callingId, setCallingId] = useState<string | null>(null);

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
              onClick={() => verify(c)}
              aria-label={`Verify ${c.full_name}'s email`}
            >
              Verify
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
    [busyId, callingId],
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
    </div>
  );
}

export default ContactsPage;
