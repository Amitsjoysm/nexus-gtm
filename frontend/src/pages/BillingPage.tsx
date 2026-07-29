import { useMemo, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Card, CardHeader, EmptyState, Icons, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import type {
  BillingCredits,
  BillingUsage,
  CapabilityUsage,
  CreditEntry,
  Invoice,
} from "@/lib/types";
import styles from "./BillingPage.module.css";

/** Cents to a display string. Money is integer cents end to end; we only format at the edge. */
function money(cents: number, currency = "USD"): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(cents / 100);
}

function periodLabel(key: string): string {
  // "2026-07" -> "July 2026". Falls back to the raw key for any unexpected shape.
  const match = /^(\d{4})-(\d{2})$/.exec(key);
  if (!match) return key;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, 1);
  return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

/**
 * A quota meter. The bar communicates one thing: how close this workspace is to a limit.
 * Colour is state, not decoration — neutral until the soft limit, warning past it, danger at
 * or over quota.
 */
function UsageMeter({ row }: { row: CapabilityUsage }) {
  const unlimited = row.quota == null;
  const pct = unlimited ? 0 : Math.min(100, (row.used / Math.max(row.quota!, 1)) * 100);
  const tone = unlimited ? "none" : pct >= 100 ? "danger" : pct >= 80 ? "warn" : "ok";

  return (
    <li className={styles.meter}>
      <div className={styles.meterHead}>
        <span className={styles.meterName}>{row.name}</span>
        <span className={cn(styles.meterCount, tone === "danger" && styles.meterCountDanger)}>
          {formatNumber(row.used)}
          {unlimited ? (
            <span className={styles.meterLimit}> used</span>
          ) : (
            <span className={styles.meterLimit}> of {formatNumber(row.quota!)}</span>
          )}
        </span>
      </div>
      {unlimited ? (
        <p className={styles.meterUnlimited}>Unlimited on your plan</p>
      ) : (
        <div
          className={styles.track}
          role="progressbar"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${row.name}: ${row.used} of ${row.quota} ${row.unit}s used`}
        >
          <div className={cn(styles.fill, styles[`fill_${tone}`])} style={{ width: `${pct}%` }} />
        </div>
      )}
    </li>
  );
}

export function BillingPage() {
  const api = useApiClient();
  const usage = useApi<BillingUsage>((signal) => api.billingUsage(signal), []);
  const credits = useApi<BillingCredits>((signal) => api.billingCredits(signal), []);
  const invoices = useApi<Invoice[]>((signal) => api.billingInvoices(signal), []);
  const [openInvoice, setOpenInvoice] = useState<string | null>(null);

  const metered = useMemo(
    () => (usage.data?.capabilities ?? []).filter((c) => c.quota != null || c.used > 0),
    [usage.data],
  );

  const creditColumns: Column<CreditEntry>[] = [
    {
      key: "created_at",
      header: "Date",
      width: "160px",
      render: (r) => new Date(r.created_at).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      }),
    },
    { key: "reason", header: "Reason", render: (r) => r.reason || r.kind },
    {
      key: "delta",
      header: "Credits",
      align: "right",
      width: "120px",
      render: (r) => (
        <span className={r.delta < 0 ? styles.spend : styles.grant}>
          {r.delta > 0 ? "+" : ""}
          {formatNumber(r.delta)}
        </span>
      ),
    },
  ];

  const invoiceColumns: Column<Invoice>[] = [
    { key: "period_key", header: "Period", render: (r) => periodLabel(r.period_key) },
    {
      key: "number",
      header: "Invoice",
      render: (r) => <span className={styles.mono}>{r.number || "—"}</span>,
      hideOnMobile: true,
    },
    {
      key: "status",
      header: "Status",
      width: "130px",
      render: (r) => (
        <Badge tone={r.status === "paid" ? "success" : r.status === "void" ? "neutral" : "info"}>
          {r.status}
        </Badge>
      ),
    },
    {
      key: "total_cents",
      header: "Total",
      align: "right",
      width: "120px",
      render: (r) => <span className={styles.mono}>{money(r.total_cents, r.currency)}</span>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="Billing"
        description="Your plan, what this workspace has used this period, and your invoices."
      />

      <div className={styles.stack}>
        <Card padding="lg">
          <DataState
            state={usage}
            errorTitle="Couldn't load your plan"
            skeleton={<Skeleton width="100%" height={72} />}
          >
            {(data) => (
              <div className={styles.summary}>
                <div className={styles.summaryItem}>
                  <span className={styles.summaryLabel}>Plan</span>
                  <span className={styles.summaryValue}>
                    {data.plan_name ?? "No plan assigned"}
                  </span>
                </div>
                <div className={styles.summaryItem}>
                  <span className={styles.summaryLabel}>Billing period</span>
                  <span className={styles.summaryValue}>{periodLabel(data.period)}</span>
                </div>
                <div className={styles.summaryItem}>
                  <span className={styles.summaryLabel}>Credit balance</span>
                  <span className={styles.summaryValue}>
                    {credits.data ? formatNumber(credits.data.balance) : "—"}
                  </span>
                </div>
              </div>
            )}
          </DataState>
        </Card>

        <Card padding="lg">
          <CardHeader
            title="Usage this period"
            subtitle="Counts update as your workspace works. Limits reset when the period rolls over."
          />
          <DataState
            state={usage}
            errorTitle="Couldn't load usage"
            skeleton={
              <div className={styles.meters}>
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} width="100%" height={48} />
                ))}
              </div>
            }
            isEmpty={() => metered.length === 0}
            empty={
              <EmptyState
                icon={<Icons.BoltIcon />}
                title="Nothing used yet"
                description="Run an agent, draft a message, or verify a contact and usage will appear here."
              />
            }
          >
            {() => (
              <ul className={styles.meters}>
                {metered.map((row) => (
                  <UsageMeter key={row.capability_id} row={row} />
                ))}
              </ul>
            )}
          </DataState>
        </Card>

        <Card padding="lg">
          <CardHeader
            title="Credits"
            subtitle="Every grant and every charge, newest first."
          />
          <DataState
            state={credits}
            errorTitle="Couldn't load credits"
            skeleton={<Skeleton width="100%" height={120} />}
            isEmpty={(d) => d.entries.length === 0}
            empty={
              <EmptyState
                title="No credit activity"
                description="Credits granted with your plan and any adjustments will be listed here."
              />
            }
          >
            {(data) => (
              <DataTable
                columns={creditColumns}
                rows={data.entries}
                getRowKey={(r) => r.id}
                caption="Credit ledger"
              />
            )}
          </DataState>
        </Card>

        <Card padding="lg">
          <CardHeader title="Invoices" subtitle="Select an invoice to see its charges." />
          <DataState
            state={invoices}
            errorTitle="Couldn't load invoices"
            skeleton={<Skeleton width="100%" height={120} />}
            isEmpty={(d) => d.length === 0}
            empty={
              <EmptyState
                title="No invoices yet"
                description="Your first invoice appears once a billing period closes."
              />
            }
          >
            {(rows) => (
              <>
                <DataTable
                  columns={invoiceColumns}
                  rows={rows}
                  getRowKey={(r) => r.id}
                  onRowClick={(r) => setOpenInvoice(openInvoice === r.id ? null : r.id)}
                  caption="Invoices"
                />
                {openInvoice && (
                  <div className={styles.lines}>
                    <h3 className={styles.linesTitle}>
                      Charges on{" "}
                      {periodLabel(rows.find((r) => r.id === openInvoice)?.period_key ?? "")}
                    </h3>
                    <ul className={styles.lineList}>
                      {(rows.find((r) => r.id === openInvoice)?.lines ?? []).map((ln, i) => (
                        <li key={`${ln.kind}-${i}`} className={styles.line}>
                          <span className={styles.lineDesc}>{ln.description}</span>
                          <span className={cn(styles.mono, styles.lineAmount)}>
                            {money(
                              ln.amount_cents,
                              rows.find((r) => r.id === openInvoice)?.currency,
                            )}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </DataState>
        </Card>
      </div>
    </div>
  );
}

export default BillingPage;
