import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, DataTable, EmptyState, ErrorState, Icons, Input, Skeleton } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { WorkspaceContact } from "@/lib/types";
import styles from "./ContactsPage.module.css";

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  valid: "success",
  risky: "warning",
  unknown: "neutral",
  invalid: "danger",
};

export function ContactsPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const contacts = useApi<WorkspaceContact[]>((signal) => api.listWorkspaceContacts(undefined, signal), []);
  const [query, setQuery] = useState("");

  const rows = contacts.data ?? [];
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((c) =>
      [c.full_name, c.title, c.email, c.account_name].some((v) => (v ?? "").toLowerCase().includes(q)),
    );
  }, [rows, query]);

  const columns: Column<WorkspaceContact>[] = useMemo(
    () => [
      { key: "full_name", header: "Name", render: (c) => <span className={styles.name}>{c.full_name}</span> },
      { key: "title", header: "Title", render: (c) => c.title ?? <span className={styles.muted}>—</span> },
      {
        key: "account_name",
        header: "Account",
        render: (c) => <span className={styles.account}>{c.account_name}</span>,
      },
      {
        key: "email",
        header: "Email",
        hideOnMobile: true,
        render: (c) =>
          c.email ? <span className={styles.mono}>{c.email}</span> : <span className={styles.muted}>—</span>,
      },
      {
        key: "email_status",
        header: "Status",
        render: (c) =>
          c.email_status ? (
            <Badge tone={STATUS_TONE[c.email_status] ?? "neutral"} dot>
              {c.email_status}
            </Badge>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
    ],
    [],
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
        <span className={styles.count}>
          {visible.length} of {rows.length}
        </span>
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
    </div>
  );
}

export default ContactsPage;
