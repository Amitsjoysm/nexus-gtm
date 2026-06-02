import { useMemo, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Icons,
  Select,
  Skeleton,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { humanize, timeAgo } from "@/lib/format";
import { strengthMeta } from "@/lib/display";
import type { SignalEvent } from "@/lib/types";
import styles from "./SignalsPage.module.css";

export function SignalsPage() {
  const api = useApiClient();
  const signals = useApi<SignalEvent[]>(
    (signal) => api.listSignals({ limit: 100 }, signal),
    [],
  );
  const [kind, setKind] = useState("");

  const kinds = useMemo(() => {
    const set = new Set((signals.data ?? []).map((s) => s.kind));
    return Array.from(set).sort();
  }, [signals.data]);

  return (
    <div>
      <PageHeader
        title="Signals"
        description="Every buying signal detected across your accounts, ranked by strength."
        actions={
          <Button
            variant="secondary"
            iconLeft={<Icons.RefreshIcon />}
            onClick={signals.refetch}
          >
            Refresh
          </Button>
        }
      />

      <DataState
        state={signals}
        skeleton={
          <Card padding="md">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} style={{ padding: "12px 0" }}>
                <Skeleton width="60%" height={13} />
                <div style={{ height: 6 }} />
                <Skeleton width="35%" height={10} />
              </div>
            ))}
          </Card>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.SignalIcon />}
            title="No signals yet"
            description="As soon as we detect buying intent on your accounts, it shows up here."
          />
        }
      >
        {(rows) => {
          const visible = kind ? rows.filter((s) => s.kind === kind) : rows;
          return (
            <>
              <div className={styles.toolbar}>
                <div className={styles.filter}>
                  <Select
                    aria-label="Filter by signal type"
                    value={kind}
                    onChange={(e) => setKind(e.target.value)}
                    options={[
                      { value: "", label: "All signal types" },
                      ...kinds.map((k) => ({ value: k, label: humanize(k) })),
                    ]}
                  />
                </div>
                <span className={styles.count}>
                  {visible.length} signal{visible.length === 1 ? "" : "s"}
                </span>
              </div>

              <Card padding="md">
                {visible.length === 0 ? (
                  <EmptyState
                    compact
                    icon={<Icons.SearchIcon />}
                    title="No matching signals"
                    description="Try a different signal type."
                  />
                ) : (
                  <div className={styles.list}>
                    {visible.map((sig) => {
                      const meta = strengthMeta(sig.strength);
                      return (
                        <div key={sig.id} className={styles.signal}>
                          <Badge tone={meta.tone} dot>
                            {meta.label}
                          </Badge>
                          <div className={styles.body}>
                            <div className={styles.title}>{sig.title}</div>
                            {sig.body && <div className={styles.text}>{sig.body}</div>}
                            <div className={styles.meta}>
                              <span>{humanize(sig.kind)}</span>
                              <span>·</span>
                              <span>{sig.source}</span>
                              <span>·</span>
                              <span>{timeAgo(sig.occurred_at)}</span>
                              {sig.url && (
                                <>
                                  <span>·</span>
                                  <a
                                    className={styles.link}
                                    href={sig.url}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    Source
                                  </a>
                                </>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Card>
            </>
          );
        }}
      </DataState>
    </div>
  );
}
