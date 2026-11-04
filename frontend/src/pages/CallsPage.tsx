import { useMemo, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  Icons,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { CallConsole } from "@/components/CallConsole";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { formatDate } from "@/lib/format";
import type { CallTask } from "@/lib/types";
import styles from "./CallsPage.module.css";

/**
 * The call queue, and the call itself. They are never on screen together (6.3, 2026-10-29): the
 * queue is what the rep picks from BETWEEN calls, and once a call is open the console takes the
 * whole screen, so nothing the rep needs during it sits below a list of other people. Logging the
 * outcome, or Esc, comes back to the queue.
 */
export function CallsPage() {
  const api = useApiClient();
  const queue = useApi<CallTask[]>((signal) => api.callQueue("open", false, signal), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const rows = queue.data ?? [];
  const selected = useMemo(() => rows.find((t) => t.id === selectedId) ?? null, [rows, selectedId]);

  const columns: Column<CallTask>[] = useMemo(
    () => [
      {
        key: "contact_name",
        header: "Contact",
        sortValue: (t) => t.contact_name,
        render: (t) => (
          <span className={styles.person}>
            <span className={styles.name}>{t.contact_name ?? "—"}</span>
            {t.title && <span className={styles.muted}>{t.title}</span>}
          </span>
        ),
      },
      {
        key: "account_name",
        header: "Account",
        sortValue: (t) => t.account_name,
        render: (t) => <span className={styles.muted}>{t.account_name}</span>,
      },
      {
        key: "reason",
        header: "Why",
        hideOnMobile: true,
        render: (t) => <span className={styles.reason}>{t.reason || "—"}</span>,
      },
      {
        key: "phone",
        header: "Phone",
        hideOnMobile: true,
        sortValue: (t) => t.phone,
        render: (t) =>
          t.phone ? <span className={styles.mono}>{t.phone}</span> : <span className={styles.muted}>no number</span>,
      },
      {
        key: "due_at",
        header: "Due",
        hideOnMobile: true,
        sortValue: (t) => t.due_at,
        render: (t) => <span className={styles.muted}>{t.due_at ? formatDate(t.due_at) : "Now"}</span>,
      },
      {
        key: "priority",
        header: "Priority",
        align: "right",
        sortValue: (t) => t.priority,
        render: (t) => (
          <Badge tone={t.priority >= 70 ? "success" : t.priority >= 40 ? "warning" : "neutral"}>
            {t.priority}
          </Badge>
        ),
      },
    ],
    [],
  );

  if (selected) {
    return (
      <CallConsole
        task={selected}
        layout="screen"
        autoGenerate
        onExit={() => setSelectedId(null)}
        onLogged={() => {
          setSelectedId(null);
          queue.refetch();
        }}
      />
    );
  }

  return (
    <div>
      <PageHeader
        title="Calls"
        description="Your prioritized call list. Open a call for the person, the script and the outcome on one screen."
        actions={<span className={styles.count}>{rows.length} to call</span>}
      />

      <Card padding="none" className={styles.queue}>
        {queue.error && !queue.data ? (
          <ErrorState title="Couldn't load the call queue" message={queue.error.detail} onRetry={queue.refetch} />
        ) : (
          <DataTable<CallTask>
            columns={columns}
            rows={rows}
            getRowKey={(t) => t.id}
            loading={queue.loading && !queue.data}
            skeletonRows={6}
            onRowClick={(t) => setSelectedId(t.id)}
            caption="Call queue. Open a call to start it."
            empty={
              <EmptyState
                icon={<Icons.PhoneIcon />}
                title="No calls queued"
                description="Calls appear here from campaigns with a call step, or from the Call button on a contact."
              />
            }
          />
        )}
      </Card>
    </div>
  );
}

export default CallsPage;
