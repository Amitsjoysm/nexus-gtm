import { useEffect, useMemo, useRef, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Card, CardHeader, EmptyState, Skeleton, useToast } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { PlanPicker } from "./billing/PlanPicker";
import { CreditUsageReport } from "./billing/CreditUsageReport";
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

/** Whole days from now until `iso`, negative once it has passed. */
function daysUntil(iso: string): number {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return 0;
  return Math.ceil((then - Date.now()) / 86_400_000);
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** "Oct 23 – Nov 23, 2026": a paid period, which for an annual plan is a year, not a month. */
function dateRange(start: string, end: string): string {
  return `${shortDate(start)} – ${shortDate(end)}`;
}

/**
 * What an invoice bills. A Stripe invoice's `period_key` is `stripe:<id>`, which the list showed as
 * its period (2026-10-23), so its service period comes first, then the day it was issued.
 */
function invoicePeriod(inv: Invoice): string {
  if (inv.period_start && inv.period_end) return dateRange(inv.period_start, inv.period_end);
  if (/^\d{4}-\d{2}$/.test(inv.period_key)) return periodLabel(inv.period_key);
  return inv.issued_at ? `Issued ${shortDate(inv.issued_at)}` : "—";
}

/** How long to wait for Stripe's word after Checkout returns: its webhooks usually land in seconds. */
const CONFIRM_ATTEMPTS = 30;
const CONFIRM_EVERY_MS = 2000;

/**
 * The one line about this subscription the customer needs before reading anything else.
 *
 * Rendered only when there is something to say. A permanent "everything is fine" strip trains
 * people to ignore the space, and then the message that matters arrives in a place nobody looks.
 * A paused workspace especially: every meter below reads zero, and without this that looks like
 * a bug rather than a deliberate state.
 */
function SubscriptionNotice({ data }: { data: BillingUsage }) {
  const trialDays = data.trial_end ? daysUntil(data.trial_end) : null;

  let tone: "warn" | "danger" | "info" | null = null;
  let headline = "";
  let detail = "";

  if (data.status === "suspended") {
    tone = "warn";
    headline = "This workspace is paused";
    detail =
      "Billing is stopped and features are unavailable. Your plan, usage history and data are kept. Contact your account manager to resume.";
  } else if (data.status === "past_due") {
    tone = "danger";
    headline = "A payment did not go through";
    detail =
      "We will retry automatically. Update your card in the customer portal to avoid interruption.";
  } else if (data.status === "trialing" && trialDays !== null) {
    tone = trialDays <= 3 ? "warn" : "info";
    headline =
      trialDays <= 0
        ? "Your trial has ended"
        : `Your trial ends in ${trialDays} day${trialDays === 1 ? "" : "s"}`;
    detail =
      data.trial_end != null
        ? `On ${shortDate(data.trial_end)} this workspace moves to ${
            data.plan_name ?? "your plan"
          } if a payment method is on file, or is cancelled if not.`
        : "";
  }

  if (tone === null) return null;

  return (
    <div className={cn(styles.notice, styles[`notice_${tone}`])} role="status">
      <p className={styles.noticeTitle}>{headline}</p>
      {detail && <p className={styles.noticeBody}>{detail}</p>}
    </div>
  );
}

/**
 * Mid-cycle plan changes already committed to this period's invoice.
 *
 * Shown before the invoice exists, because "why is my bill different this month" is exactly the
 * question this answers, and answering it after the charge is a support conversation instead.
 */
function ProrationCard({ data }: { data: BillingUsage }) {
  if (data.proration_lines.length === 0) return null;
  const net = data.pending_proration_cents;

  return (
    <Card padding="lg">
      <CardHeader
        title="Plan change this period"
        subtitle="Charged for the days you used, credited for the days you did not."
      />
      <ul className={styles.lineList}>
        {data.proration_lines.map((ln, i) => (
          <li key={`${ln.kind}-${i}`} className={styles.line}>
            <span className={styles.lineDesc}>{ln.description}</span>
            <span className={cn(styles.mono, styles.lineAmount)}>{money(ln.amount_cents)}</span>
          </li>
        ))}
        <li className={cn(styles.line, styles.lineNet)}>
          <span className={styles.lineDesc}>
            {net >= 0 ? "Added to this period" : "Credited to this period"}
          </span>
          <span className={cn(styles.mono, styles.lineAmount)}>{money(net)}</span>
        </li>
      </ul>
    </Card>
  );
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

/**
 * Capabilities that measure a LEVEL rather than an action, and so keep a hard quota.
 *
 * Mirrors `meter_kind == "gauge"` in the billing catalog. Held here as a small literal rather than
 * derived from the response because `CapabilityUsage` does not carry the meter kind, and widening
 * that payload to render one card is a worse trade than a three-item list that a test pins.
 */
const GAUGES = new Set(["seat.member", "platform.storage", "network.persons"]);

export function BillingPage() {
  const api = useApiClient();
  const usage = useApi<BillingUsage>((signal) => api.billingUsage(signal), []);
  const credits = useApi<BillingCredits>((signal) => api.billingCredits(signal), []);
  const invoices = useApi<Invoice[]>((signal) => api.billingInvoices(signal), []);
  const [openInvoice, setOpenInvoice] = useState<string | null>(null);
  const toast = useToast();

  // Back from Stripe Checkout (2026-10-23). The page ignored `?checkout=`, and Stripe's redirect
  // often beats its webhook, so a customer who had just paid could see Free and no word that the
  // payment went through. Wait for Stripe's word, say what happened, and consume the parameter so a
  // refresh does not say it again.
  const [confirming, setConfirming] = useState<"waiting" | "late" | null>(null);
  const returned = useRef(false);
  const { refetch: refetchUsage } = usage;
  const { refetch: refetchCredits } = credits;
  const { refetch: refetchInvoices } = invoices;
  useEffect(() => {
    if (returned.current) return;
    returned.current = true;
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("checkout");
    if (!outcome) return;
    params.delete("checkout");
    const rest = params.toString();
    window.history.replaceState(window.history.state, "",
      `${window.location.pathname}${rest ? `?${rest}` : ""}${window.location.hash}`);
    if (outcome === "cancelled") {
      toast.toast({ title: "Checkout cancelled", description: "Nothing was charged." });
      return;
    }
    if (outcome !== "success") return;
    setConfirming("waiting");
    // Not cancelled on unmount: the ref above runs it once, and it ends by itself within a minute.
    void (async () => {
      for (let attempt = 0; attempt < CONFIRM_ATTEMPTS; attempt += 1) {
        try {
          const now = await api.billingUsage();
          if (now.switch_in_place) {
            setConfirming(null);
            refetchUsage();
            refetchCredits();
            refetchInvoices();
            toast.success(`You're on ${now.plan_name ?? "your new plan"}`,
              "Your payment went through and your credits are in your balance.");
            return;
          }
        } catch {
          // A blip while waiting is not the answer; keep asking until the time is up.
        }
        await new Promise((r) => window.setTimeout(r, CONFIRM_EVERY_MS));
      }
      setConfirming("late");
    })();
  }, [api, toast, refetchUsage, refetchCredits, refetchInvoices]);

  // GAUGES ONLY. This card used to list every capability with usage, drawing a "40 of 500" bar
  // against the plan quota. Under credits-only billing that quota is no longer what gates a priced
  // action — the balance is — so a workspace with an empty balance saw four green meters beside a
  // product that had stopped working, and the meters were the more prominent of the two.
  //
  // A quota is still the literal truth for gauges: seats and stored gigabytes are LEVELS, they
  // never draw on credits, and going over one is enforced as a hard cap. Everything else moved to
  // `CreditUsageReport`, which reports what is actually being spent.
  const limits = useMemo(
    () => (usage.data?.capabilities ?? []).filter((c) => GAUGES.has(c.capability_id)),
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
    { key: "period_key", header: "Period", render: (r) => invoicePeriod(r) },
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
              <>
                {confirming && (
                  <div className={cn(styles.notice, styles.notice_info)} role="status">
                    <p className={styles.noticeTitle}>
                      {confirming === "waiting"
                        ? "Confirming your payment with Stripe…"
                        : "Your payment went through at Stripe"}
                    </p>
                    <p className={styles.noticeBody}>
                      {confirming === "waiting"
                        ? "This usually takes a few seconds. Your new plan and its credits appear here as soon as Stripe confirms it."
                        : "Your plan updates here as soon as Stripe confirms it, usually within a minute. There is no need to pay again: refresh this page in a moment."}
                    </p>
                  </div>
                )}
                <SubscriptionNotice data={data} />
                <div className={styles.summary}>
                  <div className={styles.summaryItem}>
                    <span className={styles.summaryLabel}>Plan</span>
                    <span className={styles.summaryValue}>
                      {data.plan_name ?? "No plan assigned"}
                      {data.status === "suspended" && (
                        <Badge tone="warning" dot className={styles.summaryBadge}>
                          paused
                        </Badge>
                      )}
                    </span>
                  </div>
                  <div className={styles.summaryItem}>
                    <span className={styles.summaryLabel}>Billing period</span>
                    <span className={styles.summaryValue}>
                      {/* The paid period itself: an annual plan's is a year, not this month. */}
                      {data.period_start && data.period_end
                        ? dateRange(data.period_start, data.period_end)
                        : periodLabel(data.period)}
                    </span>
                  </div>
                  <div className={styles.summaryItem}>
                    <span className={styles.summaryLabel}>
                      {data.status === "trialing"
                        ? "Trial ends"
                        : data.cancel_at_period_end
                          ? "Ends"
                          : "Renews"}
                    </span>
                    <span className={styles.summaryValue}>
                      {data.status === "trialing"
                        ? data.trial_end
                          ? shortDate(data.trial_end)
                          : "No end date"
                        : data.period_end
                          ? shortDate(data.period_end)
                          : "—"}
                    </span>
                  </div>
                  <div className={styles.summaryItem}>
                    <span className={styles.summaryLabel}>Credit balance</span>
                    <span className={styles.summaryValue}>
                      {credits.data ? formatNumber(credits.data.balance) : "—"}
                    </span>
                  </div>
                </div>
              </>
            )}
          </DataState>
        </Card>

        {usage.data && <ProrationCard data={usage.data} />}

        {/* Above usage and invoices: someone who came here from a locked nav item is here to
            change plan, not to read a meter. */}
        <PlanPicker
          usage={usage.data ?? null}
          onChanged={() => {
            usage.refetch();
            credits.refetch();
            invoices.refetch();
          }}
        />

        {/* The answer to "where did my credits go?", above the plan limits: under credits-only
            billing the balance is what stops a call, so it is what the customer came to read. */}
        <CreditUsageReport />

        <Card padding="lg">
          <CardHeader
            title="Plan limits"
            subtitle="Seats and storage are capped by your plan rather than charged in credits."
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
            isEmpty={() => limits.length === 0}
            empty={
              <EmptyState
                title="No capped resources"
                description="Your plan does not cap seats or storage."
              />
            }
          >
            {() => (
              <ul className={styles.meters}>
                {limits.map((row) => (
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
                {openInvoice && (() => {
                  const inv = rows.find((r) => r.id === openInvoice);
                  if (!inv) return null;
                  return (
                    <div className={styles.lines}>
                      <h3 className={styles.linesTitle}>Charges for {invoicePeriod(inv)}</h3>
                      {inv.lines.length > 0 ? (
                        <ul className={styles.lineList}>
                          {inv.lines.map((ln, i) => (
                            <li key={`${ln.kind}-${i}`} className={styles.line}>
                              <span className={styles.lineDesc}>{ln.description}</span>
                              <span className={cn(styles.mono, styles.lineAmount)}>
                                {money(ln.amount_cents, inv.currency)}
                              </span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className={styles.noticeBody}>
                          {inv.hosted_url
                            ? "The charges are listed on Stripe's copy of this invoice."
                            : "No charges are listed for this invoice."}
                        </p>
                      )}
                      {/* Stripe's own copy and its PDF, the document a customer files. Shown only
                          when there is one: a link that opens nothing is worse than none. */}
                      {(inv.hosted_url || inv.pdf_url) && (
                        <p className={styles.invoiceLinks}>
                          {inv.hosted_url && (
                            <a href={inv.hosted_url} target="_blank" rel="noreferrer noopener">
                              View invoice
                            </a>
                          )}
                          {inv.pdf_url && (
                            <a href={inv.pdf_url} target="_blank" rel="noreferrer noopener">
                              Download PDF
                            </a>
                          )}
                        </p>
                      )}
                    </div>
                  );
                })()}
              </>
            )}
          </DataState>
        </Card>
      </div>
    </div>
  );
}

export default BillingPage;
