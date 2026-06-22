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
import type { CallTask } from "@/lib/types";
import styles from "./CallsPage.module.css";

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
        render: (t) => <span className={styles.name}>{t.contact_name ?? "—"}</span>,
      },
      {
        key: "account_name",
        header: "Account",
        sortValue: (t) => t.account_name,
        render: (t) => <span className={styles.muted}>{t.account_name}</span>,
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

  return (
    <div>
      <PageHeader
        title="Calls"
        description="Your prioritized call list. Open a call for an AI script, dial, and log the outcome."
        actions={<span className={styles.count}>{rows.length} to call</span>}
      />

      <div className={styles.layout}>
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
              caption="Call queue"
              empty={
                <EmptyState
                  icon={<Icons.PhoneIcon />}
                  title="No calls queued"
                  description="Calls appear here from cadences with a call step, or from the Call button on a contact."
                />
              }
            />
          )}
        </Card>

        <Card className={styles.panel}>
          {selected ? (
            <CallConsole
              task={selected}
              autoGenerate
              onLogged={() => {
                setSelectedId(null);
                queue.refetch();
              }}
            />
          ) : (
            <div className={styles.placeholder}>
              <Icons.PhoneIcon />
              <p>Select a call to see the AI script and log the outcome.</p>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

export default CallsPage;
