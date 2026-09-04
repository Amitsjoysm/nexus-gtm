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
import { useSignalWindow } from "@/app/SignalWindowContext";
import { humanize, timeAgo } from "@/lib/format";
import { signalSourceMeta, strengthMeta } from "@/lib/display";
import type { SignalEvent } from "@/lib/types";
import styles from "./SignalsPage.module.css";

export function SignalsPage() {
  const api = useApiClient();
  const { windowDays } = useSignalWindow();
  /**
   * Events only, by default.
   *
   * `_classify_news` already tiers correctly — funding 0.85, a leadership change 0.6, a company's
   * own marketing post 0.4 — and 0.5 is the same floor the alert subscriber uses, so the list now
   * agrees with the Inbox about what counts as an event. Measured live: 120 of 134 RSS signals
   * were 0.4 blog posts sitting on top of the four that mattered.
   *
   * A toggle, not a silent filter: hidden rows are counted and one click brings them back, because
   * a page that quietly drops data is how someone concludes collection is broken.
   */
  const [eventsOnly, setEventsOnly] = useState(true);
  const signals = useApi<SignalEvent[]>(
    (signal) =>
      api.listSignals(
        {
          limit: 100,
          max_age_days: windowDays ?? undefined,
          min_strength: eventsOnly ? 0.5 : undefined,
        },
        signal,
      ),
    [windowDays, eventsOnly],
  );
  // Fetched unfiltered so the toggle can say how many it is hiding. Cheap: the same 100-row page
  // the list already asks for, and only while the filter is on.
  const allSignals = useApi<SignalEvent[]>(
    (signal) =>
      eventsOnly
        ? api.listSignals({ limit: 100, max_age_days: windowDays ?? undefined }, signal)
        : Promise.resolve([]),
    [windowDays, eventsOnly],
  );
  const hiddenCount = eventsOnly
    ? Math.max(0, (allSignals.data?.length ?? 0) - (signals.data?.length ?? 0))
    : 0;
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
          <>
            {(eventsOnly ? hiddenCount > 0 : true) && (
              <Button
                variant="ghost"
                onClick={() => setEventsOnly((v) => !v)}
                aria-pressed={!eventsOnly}
              >
                {eventsOnly
                  ? `Show ${hiddenCount} weaker mention${hiddenCount === 1 ? "" : "s"}`
                  : "Events only"}
              </Button>
            )}
            <Button
              variant="secondary"
              iconLeft={<Icons.RefreshIcon />}
              onClick={signals.refetch}
            >
              Refresh
            </Button>
          </>
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
            title={windowDays !== null ? `No signals in the last ${windowDays} days` : "No signals yet"}
            description={
              windowDays !== null
                ? "Widen the signal window in the top bar to see older activity."
                : "As soon as we detect buying intent on your accounts, it shows up here."
            }
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
                  {windowDays !== null && ` · last ${windowDays} days`}
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
                      const src = signalSourceMeta(sig);
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
                              <span title={src.hint}>
                                {src.label}
                                {src.isSynthetic && " (sample)"}
                              </span>
                              <span>·</span>
                              <span>{timeAgo(sig.occurred_at)}</span>
                              <span>·</span>
                              <a
                                className={styles.link}
                                href={src.href}
                                target="_blank"
                                rel="noreferrer"
                                title={src.hint}
                              >
                                {src.linkLabel} ↗
                              </a>
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
