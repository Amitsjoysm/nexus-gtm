import { useState } from "react";
import { Button } from "@/components/ui";
import { CustomPlanDialog } from "./admin/CustomPlanDialog";
import { TenantActionsDialog } from "./admin/TenantActionsDialog";
import { UserActionsDialog } from "./admin/UserActionsDialog";
import { RateCardDialog } from "./admin/RateCardDialog";
import { PlanEntitlementsDialog } from "./admin/PlanEntitlementsDialog";
import { PlatformAdmins } from "./admin/PlatformAdmins";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Card, CardHeader, EmptyState, Skeleton, Tabs, useToast } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { usePlatformCan } from "@/app/RequirePlatformAdmin";
import {
  ADMINS_MANAGE,
  PRICING_WRITE,
  SUBSCRIPTIONS_WRITE,
  USERS_IMPERSONATE,
  USERS_MANAGE,
  PROVIDERS_MANAGE,
  FEATURES_MANAGE,
} from "@/lib/permissions";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import type {
  AdminPlan,
  AdminRateCard,
  AdminSubscription,
  FeatureFlag,
  PlatformOverview,
  RevenueReport,
} from "@/lib/types";
import { ApiError } from "@/lib/api";
import { CustomersTab } from "./admin/CustomersTab";
import { PaymentsTab } from "./admin/PaymentsTab";
import { ProviderKeysTab } from "./admin/ProviderKeysTab";
import { RuntimeConfigTab } from "./admin/RuntimeConfigTab";
import { FeatureSwitchesTab } from "./admin/FeatureSwitchesTab";
import styles from "./AdminBillingPage.module.css";

const MARGIN_FLOOR = 0.5;

function money(cents: number, currency = "USD"): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(cents / 100);
}

/** Credits are the pricing unit: 1 credit = $0.01 list. */
function creditsToUsd(credits: number): string {
  return `$${(credits / 100).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}`;
}

function MarginCell({ margin, exception }: { margin: number; exception: boolean }) {
  const below = margin < MARGIN_FLOOR;
  return (
    <span className={styles.marginCell}>
      <span
        className={cn(
          styles.mono,
          below && !exception && styles.marginBad,
          below && exception && styles.marginException,
        )}
      >
        {(margin * 100).toFixed(0)}%
      </span>
      {below && exception && (
        <Badge tone="warning" title="Finance recorded an explicit exception">
          exception
        </Badge>
      )}
    </span>
  );
}

function RateCards() {
  const api = useApiClient();
  const can = usePlatformCan();
  const rates = useApi<AdminRateCard[]>((signal) => api.adminBillingRates(signal), []);
  const [editing, setEditing] = useState<AdminRateCard | null>(null);
  // Repricing is `pricing.write`. A support admin reads this tab and cannot change it — the
  // server enforces that regardless; hiding the control just stops offering an action that 403s.
  const canReprice = can(PRICING_WRITE);

  const columns: Column<AdminRateCard>[] = [
    {
      key: "name",
      header: "Capability",
      render: (r) => (
        <div className={styles.stackCell}>
          <span>{r.name}</span>
          <span className={styles.subtle}>{r.capability_id}</span>
        </div>
      ),
      sortable: true,
      sortValue: (r) => r.name,
    },
    {
      key: "category",
      header: "Category",
      width: "130px",
      hideOnMobile: true,
      sortable: true,
    },
    {
      key: "credits_per_unit",
      header: "Price",
      align: "right",
      width: "150px",
      render: (r) => (
        <span className={styles.mono}>
          {formatNumber(r.credits_per_unit)} cr
          <span className={styles.subtle}> · {creditsToUsd(r.credits_per_unit)}</span>
        </span>
      ),
      sortable: true,
      sortValue: (r) => r.credits_per_unit,
    },
    {
      key: "unit_cost_usd",
      header: "COGS",
      align: "right",
      width: "110px",
      hideOnMobile: true,
      render: (r) => <span className={styles.mono}>${r.unit_cost_usd.toFixed(4)}</span>,
      sortable: true,
      sortValue: (r) => r.unit_cost_usd,
    },
    {
      key: "gross_margin",
      header: "Margin",
      align: "right",
      width: "140px",
      render: (r) => <MarginCell margin={r.gross_margin} exception={r.margin_exception} />,
      sortable: true,
      sortValue: (r) => r.gross_margin,
    },
    ...(canReprice
      ? [
          {
            key: "actions",
            header: "",
            width: "110px",
            render: (r: AdminRateCard) => (
              <div className={styles.rowActions}>
                <Button variant="secondary" onClick={() => setEditing(r)}>
                  Reprice
                </Button>
              </div>
            ),
          } as Column<AdminRateCard>,
        ]
      : []),
  ];

  return (
    <DataState
      state={rates}
      errorTitle="Couldn't load rate cards"
      skeleton={<Skeleton width="100%" height={200} />}
      isEmpty={(d) => d.length === 0}
      empty={
        <EmptyState
          title="No rate cards"
          description="Rate cards are seeded on startup. If this is empty, the seed did not run."
        />
      }
    >
      {(rows) => {
        const underwater = rows.filter((r) => r.gross_margin < MARGIN_FLOOR);
        return (
          <>
            {underwater.length > 0 && (
              <p className={styles.notice} role="status">
                {underwater.length} capabilit{underwater.length === 1 ? "y is" : "ies are"} priced
                below the {MARGIN_FLOOR * 100}% margin floor. Each carries a recorded exception.
              </p>
            )}
            <DataTable
              columns={columns}
              rows={rows}
              getRowKey={(r) => r.capability_id}
              caption="Rate cards"
              minWidth={canReprice ? 880 : 760}
            />
            <RateCardDialog
              open={editing !== null}
              onClose={() => setEditing(null)}
              card={editing}
              onDone={() => rates.refetch()}
            />
          </>
        );
      }}
    </DataState>
  );
}

function Plans() {
  const api = useApiClient();
  const can = usePlatformCan();
  const plans = useApi<AdminPlan[]>((signal) => api.adminBillingPlans(signal), []);
  const [editing, setEditing] = useState<AdminPlan | null>(null);
  const canEdit = can(PRICING_WRITE);

  const columns: Column<AdminPlan>[] = [
    {
      key: "name",
      header: "Plan",
      render: (r) => (
        <div className={styles.stackCell}>
          <span>{r.name}</span>
          <span className={styles.subtle}>{r.id}</span>
        </div>
      ),
    },
    {
      key: "plan_class",
      header: "Class",
      width: "120px",
      render: (r) => <Badge tone="neutral">{r.plan_class}</Badge>,
    },
    {
      key: "status",
      header: "Status",
      width: "130px",
      render: (r) => (
        <Badge tone={r.status === "active" ? "success" : "neutral"}>{r.status}</Badge>
      ),
    },
    {
      key: "base_price_cents",
      header: "Base / mo",
      align: "right",
      width: "120px",
      render: (r) => <span className={styles.mono}>{money(r.base_price_cents, r.currency)}</span>,
    },
    {
      key: "included_credits",
      header: "Credits",
      align: "right",
      width: "110px",
      hideOnMobile: true,
      render: (r) => <span className={styles.mono}>{formatNumber(r.included_credits)}</span>,
    },
    {
      key: "entitlement_count",
      header: "Entitlements",
      align: "right",
      width: "120px",
      hideOnMobile: true,
      render: (r) => <span className={styles.mono}>{r.entitlement_count}</span>,
    },
    ...(canEdit
      ? [
          {
            key: "actions",
            header: "",
            width: "130px",
            render: (r: AdminPlan) => (
              <div className={styles.rowActions}>
                <Button variant="secondary" onClick={() => setEditing(r)}>
                  What's included
                </Button>
              </div>
            ),
          } as Column<AdminPlan>,
        ]
      : []),
  ];

  return (
    <DataState
      state={plans}
      errorTitle="Couldn't load plans"
      skeleton={<Skeleton width="100%" height={200} />}
      isEmpty={(d) => d.length === 0}
      empty={<EmptyState title="No plans" description="Plans are seeded on startup." />}
    >
      {(rows) => (
        <>
          <DataTable
            columns={columns}
            rows={rows}
            getRowKey={(r) => r.id}
            caption="Plans"
            minWidth={canEdit ? 900 : 760}
          />
          <PlanEntitlementsDialog
            open={editing !== null}
            onClose={() => {
              setEditing(null);
              plans.refetch();
            }}
            plan={editing}
          />
        </>
      )}
    </DataState>
  );
}

function Subscriptions() {
  const api = useApiClient();
  const can = usePlatformCan();
  const subs = useApi<AdminSubscription[]>((signal) => api.adminBillingSubscriptions(signal), []);
  const plans = useApi<AdminPlan[]>((signal) => api.adminBillingPlans(signal), []);
  const [custom, setCustom] = useState<AdminSubscription | null>(null);
  const [actions, setActions] = useState<AdminSubscription | null>(null);
  // "Manage" still opens for a support admin — it holds the credit grant they legitimately need.
  // A custom plan is a subscription write and nothing else, so it disappears without one.
  const canCustomPlan = can(SUBSCRIPTIONS_WRITE);

  const columns: Column<AdminSubscription>[] = [
    { key: "tenant_name", header: "Workspace", sortable: true },
    { key: "plan_id", header: "Plan", width: "180px", sortable: true },
    {
      key: "status",
      header: "Status",
      width: "130px",
      render: (r) => (
        <Badge
          tone={
            r.status === "active"
              ? "success"
              : r.status === "past_due"
                ? "danger"
                : r.status === "trialing"
                  ? "info"
                  : // A paused workspace is a deliberate state somebody must act on to undo, so it
                    // must not sit in the same grey as `canceled`.
                    r.status === "suspended"
                    ? "warning"
                    : "neutral"
          }
          dot
        >
          {r.status === "suspended" ? "paused" : r.status}
        </Badge>
      ),
    },
    {
      key: "grandfathered",
      header: "Terms",
      width: "150px",
      hideOnMobile: true,
      render: (r) =>
        r.grandfathered ? (
          <Badge tone="neutral" title="Frozen legacy terms; plan edits do not reprice this one">
            grandfathered
          </Badge>
        ) : (
          <span className={styles.subtle}>current</span>
        ),
    },
    {
      key: "actions",
      header: "",
      width: canCustomPlan ? "220px" : "120px",
      render: (r) => (
        <div className={styles.rowActions}>
          <Button variant="secondary" onClick={() => setActions(r)}>
            Manage
          </Button>
          {canCustomPlan && (
            <Button variant="ghost" onClick={() => setCustom(r)}>
              Custom plan
            </Button>
          )}
        </div>
      ),
    },
    {
      key: "current_period_end",
      header: "Period ends",
      width: "150px",
      hideOnMobile: true,
      render: (r) =>
        r.current_period_end
          ? new Date(r.current_period_end).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
              year: "numeric",
            })
          : "—",
    },
  ];

  return (
    <DataState
      state={subs}
      errorTitle="Couldn't load subscriptions"
      skeleton={<Skeleton width="100%" height={200} />}
      isEmpty={(d) => d.length === 0}
      empty={
        <EmptyState
          title="No subscriptions"
          description="Every workspace is placed on the legacy unlimited plan at startup."
        />
      }
    >
      {(rows) => (
        <>
          <DataTable
            columns={columns}
            rows={rows}
            getRowKey={(r) => r.tenant_id}
            caption="Subscriptions"
            minWidth={980}
          />
          <TenantActionsDialog
            open={actions !== null}
            onClose={() => setActions(null)}
            tenant={actions}
            plans={plans.data ?? []}
            onDone={() => subs.refetch()}
          />
          <CustomPlanDialog
            open={custom !== null}
            onClose={() => setCustom(null)}
            tenant={custom}
            plans={plans.data ?? []}
            onDone={() => subs.refetch()}
          />
        </>
      )}
    </DataState>
  );
}

/**
 * Revenue, derived at read time.
 *
 * Deliberately a definition list rather than a row of hero metric cards: the operator reading this
 * is checking figures against a finance sheet, and four big numbers in boxes make that harder. On
 * trial sits beside paying workspaces because a trial is a live logo and zero revenue, and merging
 * the two is how a pipeline number gets reported as an MRR number.
 */
/**
 * How many people are on the platform and what they consume.
 *
 * Sits above revenue because it answers a question revenue cannot: money says what was billed,
 * this says what was used. A tenant that has consumed nothing all period is a churn risk with a
 * healthy MRR line.
 *
 * Its first version reported `requests_total: 0` against a database holding 18 events —
 * `billing_usage_events` is tenant-scoped, so a cross-tenant aggregate on the RLS-bound app role
 * returns zero rows rather than raising. Fixed on the server; noted here because the number is
 * plausible when it is wrong.
 */
function PlatformOverviewPanel() {
  const api = useApiClient();
  const state = useApi<PlatformOverview>((signal) => api.adminPlatformOverview(signal), []);

  return (
    <DataState
      state={state}
      errorTitle="Couldn't load the platform overview"
      skeleton={<Skeleton width="100%" height={120} />}
    >
      {(o) => (
        <section>
          <h3 className={styles.sectionTitle}>Platform</h3>
          <dl className={styles.figures}>
            <div>
              <dt>Users</dt>
              <dd className={styles.mono}>{formatNumber(o.users)}</dd>
            </div>
            <div>
              <dt>Active users</dt>
              <dd className={cn(styles.mono, o.active_users < o.users && styles.marginBad)}>
                {formatNumber(o.active_users)}
              </dd>
            </div>
            <div>
              <dt>Workspaces</dt>
              <dd className={styles.mono}>{formatNumber(o.tenants)}</dd>
            </div>
            <div>
              <dt>Requests this period</dt>
              <dd className={styles.mono}>{formatNumber(o.requests_this_period)}</dd>
            </div>
            <div>
              <dt>Requests all time</dt>
              <dd className={styles.mono}>{formatNumber(o.requests_total)}</dd>
            </div>
            <div>
              <dt>Credits granted</dt>
              <dd className={styles.mono}>{formatNumber(o.credits_granted)}</dd>
            </div>
            <div>
              <dt>Credits spent</dt>
              <dd className={styles.mono}>{formatNumber(o.credits_spent)}</dd>
            </div>
          </dl>
          {/* Said plainly rather than left to be inferred from a gap between two numbers. */}
          <p className={styles.overviewNote}>
            {formatNumber(o.requests_with_a_user)} of {formatNumber(o.requests_total)} requests are
            attributable to a person. The rest is background work — crawls, sweeps and plays — which
            has no user to charge it to. Per-workspace consumption is on each tenant's own billing
            page.
          </p>
        </section>
      )}
    </DataState>
  );
}

function Revenue() {
  const api = useApiClient();
  const report = useApi<RevenueReport>((signal) => api.adminBillingRevenue(undefined, signal), []);

  return (
    <DataState
      state={report}
      errorTitle="Couldn't load revenue"
      skeleton={<Skeleton width="100%" height={220} />}
    >
      {(data) => {
        const r = data.revenue;
        const c = data.collection;
        const planRows = Object.entries(r.by_plan)
          .filter(([, v]) => v.tenants > 0)
          .sort((a, b) => b[1].mrr_cents - a[1].mrr_cents);
        return (
          <div className={styles.revenue}>
            <PlatformOverviewPanel />
            <section>
              <h3 className={styles.sectionTitle}>Recurring revenue</h3>
              <dl className={styles.figures}>
                <div>
                  <dt>MRR</dt>
                  <dd className={styles.mono}>{money(r.mrr_cents)}</dd>
                </div>
                <div>
                  <dt>ARR</dt>
                  <dd className={styles.mono}>{money(r.arr_cents)}</dd>
                </div>
                <div>
                  <dt>Paying workspaces</dt>
                  <dd className={styles.mono}>{formatNumber(r.paying_tenants)}</dd>
                </div>
                <div>
                  <dt>On trial</dt>
                  <dd className={styles.mono}>{formatNumber(r.trialing_tenants)}</dd>
                </div>
                <div>
                  <dt>Past due</dt>
                  <dd className={cn(styles.mono, r.past_due_tenants > 0 && styles.marginBad)}>
                    {formatNumber(r.past_due_tenants)}
                  </dd>
                </div>
              </dl>
              <p className={styles.subtle}>
                Annual plans are divided by twelve, so one annual signature does not make MRR jump a
                year and fall back the next. Past due still counts as revenue: dropping it would
                make a collection problem look like churn.
              </p>
            </section>

            <section>
              <h3 className={styles.sectionTitle}>Collection</h3>
              <dl className={styles.figures}>
                <div>
                  <dt>Invoiced</dt>
                  <dd className={styles.mono}>{money(c.invoiced_cents)}</dd>
                </div>
                <div>
                  <dt>Collected</dt>
                  <dd className={styles.mono}>{money(c.paid_cents)}</dd>
                </div>
                <div>
                  <dt>Outstanding</dt>
                  <dd className={cn(styles.mono, c.outstanding_cents > 0 && styles.marginBad)}>
                    {money(c.outstanding_cents)}
                  </dd>
                </div>
                <div>
                  <dt>Collection rate</dt>
                  <dd className={styles.mono}>{(c.collection_rate * 100).toFixed(1)}%</dd>
                </div>
                <div>
                  <dt>Failed invoices</dt>
                  <dd className={cn(styles.mono, c.failed_invoices > 0 && styles.marginBad)}>
                    {formatNumber(c.failed_invoices)}
                  </dd>
                </div>
              </dl>
              <p className={styles.subtle}>
                Draft invoices are excluded. They have not been presented to anyone, so counting
                them as uncollected would make every open period look like a failure.
              </p>
            </section>

            <section>
              <h3 className={styles.sectionTitle}>Revenue by plan</h3>
              {planRows.length === 0 ? (
                <p className={styles.subtle}>No workspace is on a priced plan yet.</p>
              ) : (
                <ul className={styles.planMix}>
                  {planRows.map(([plan, v]) => (
                    <li key={plan} className={styles.planRow}>
                      <span>{plan}</span>
                      <span className={styles.subtle}>
                        {v.tenants} workspace{v.tenants === 1 ? "" : "s"}
                      </span>
                      <span className={styles.mono}>{money(v.mrr_cents)}/mo</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        );
      }}
    </DataState>
  );
}

/**
 * Feature flags: the switch a plan entitlement hangs off.
 *
 * The Used by column is the one that matters. A flag nothing references is free to flip; one wired
 * into a paid plan turns a customer feature off, and an operator should not have to read the
 * catalog to tell those apart. An unreferenced flag says so in words, not with an empty cell.
 */
function FeatureFlags() {
  const api = useApiClient();
  const toast = useToast();
  const flags = useApi<FeatureFlag[]>((signal) => api.adminBillingFlags(signal), []);
  const [busy, setBusy] = useState<string | null>(null);

  async function toggle(flag: FeatureFlag) {
    setBusy(flag.id);
    try {
      await api.upsertFeatureFlag(flag.id, { enabled: !flag.enabled });
      toast.success(
        `${flag.id} turned ${flag.enabled ? "off" : "on"}`,
        flag.used_by_plans.length > 0
          ? `Affects ${flag.used_by_plans.join(", ")}.`
          : "No plan references this flag yet.",
      );
      flags.refetch();
    } catch (err) {
      toast.error("Couldn't change the flag", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<FeatureFlag>[] = [
    {
      key: "id",
      header: "Flag",
      render: (f) => (
        <div className={styles.stackCell}>
          <span>{f.id}</span>
          {f.description && <span className={styles.subtle}>{f.description}</span>}
        </div>
      ),
      sortable: true,
      sortValue: (f) => f.id,
    },
    {
      key: "enabled",
      header: "Default",
      width: "120px",
      render: (f) => (
        <Badge tone={f.enabled ? "success" : "neutral"} dot>
          {f.enabled ? "on" : "off"}
        </Badge>
      ),
    },
    {
      key: "used_by_plans",
      header: "Used by",
      render: (f) =>
        f.used_by_plans.length === 0 ? (
          <span className={styles.subtle}>No plan references this</span>
        ) : (
          <span>{f.used_by_plans.join(", ")}</span>
        ),
    },
    {
      key: "overrides",
      header: "Overrides",
      width: "120px",
      align: "right",
      hideOnMobile: true,
      render: (f) => {
        const keys = Object.keys(f.overrides ?? {});
        return keys.length === 0 ? (
          <span className={styles.subtle}>None</span>
        ) : (
          <span className={styles.mono} title={keys.join(", ")}>
            {keys.length}
          </span>
        );
      },
    },
    {
      key: "actions",
      header: "",
      width: "140px",
      render: (f) => (
        <div className={styles.rowActions}>
          <Button
            variant="secondary"
            loading={busy === f.id}
            onClick={() => toggle(f)}
            aria-label={`Turn ${f.id} ${f.enabled ? "off" : "on"}`}
          >
            Turn {f.enabled ? "off" : "on"}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <DataState
      state={flags}
      errorTitle="Couldn't load feature flags"
      skeleton={<Skeleton width="100%" height={200} />}
      isEmpty={(d) => d.length === 0}
      empty={
        <EmptyState
          title="No feature flags"
          description="A flag appears here once an operator sets its default. Until then, an entitlement naming it stays enabled."
        />
      }
    >
      {(rows) => (
        <>
          <p className={styles.notice} role="status">
            A flag that has never been created resolves to <strong>on</strong>. That is deliberate:
            naming a flag must never silently disable a capability a customer is paying for.
          </p>
          <DataTable
            columns={columns}
            rows={rows}
            getRowKey={(f) => f.id}
            caption="Feature flags"
            minWidth={880}
          />
        </>
      )}
    </DataState>
  );
}

/**
 * Entry point for the `admin_users` API, which shipped with no caller at all.
 *
 * Deliberately not a user *list*: there is no cross-tenant user-search endpoint, and inventing a
 * client-side one would mean pulling every user in the platform to the browser to filter. Support
 * arrives from a ticket already knowing the address, so an email field is the honest surface until
 * a real search exists server-side.
 */
function UserAdmin() {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      <p style={{ margin: 0, maxWidth: "68ch", color: "var(--text-muted)", lineHeight: 1.55 }}>
        Suspend or reactivate an account, clear a locked-out user's MFA, or open a time-boxed
        read-only session as them. Every action is recorded in the audit log with the reason you
        give.
      </p>
      <div>
        <Button onClick={() => setOpen(true)}>Manage a user</Button>
      </div>
      <UserActionsDialog open={open} onClose={() => setOpen(false)} />
    </div>
  );
}

export function AdminBillingPage() {
  const [tab, setTab] = useState("rates");
  const can = usePlatformCan();
  // Access is where power is granted, so it is visible only to admins who can grant it. The other
  // three tabs are reads, which every platform role holds.
  const tabs = [
    { value: "rates", label: "Rate cards" },
    { value: "plans", label: "Plans" },
    { value: "subs", label: "Subscriptions" },
    // Where an operator goes to answer a question about ONE customer: what are they on, what have
    // they used, and act on it. The Subscriptions tab is the roll-up; this is the drill-in.
    { value: "customers", label: "Customers" },
    { value: "revenue", label: "Revenue" },
    // Flags change what customers can do, so this follows pricing-write rather than being visible
    // to every reader.
    ...(can(PRICING_WRITE) ? [{ value: "flags", label: "Feature flags" }] : []),
    // The payment account decides WHICH BUSINESS the money lands in, which is a commercial
    // decision — so it follows pricing-write rather than being visible to every reader.
    ...(can(PRICING_WRITE) ? [{ value: "payments", label: "Payments" }] : []),
    ...(can(ADMINS_MANAGE) ? [{ value: "access", label: "Access" }] : []),
    // Provider credentials. Its own permission, not admins.manage: a holder can spend money
    // through someone else's API key, so registering one and granting platform power stay
    // separate acts.
    ...(can(PROVIDERS_MANAGE) ? [{ value: "keys", label: "Provider keys" }] : []),
    ...(can(FEATURES_MANAGE) ? [{ value: "features", label: "Feature switches" }] : []),
    // Its own tab, not folded into Provider keys: these decide whether the platform spends money
    // unattended, which is a different question from which credential it spends it with. Gated on
    // pricing-write for the same reason.
    ...(can(PRICING_WRITE) ? [{ value: "runtime", label: "Configuration" }] : []),
    // Gated on the user-admin permissions, NOT on admins.manage: `support` holds users.manage
    // and is precisely who does MFA resets, but must never reach the admin-granting surface.
    ...(can(USERS_MANAGE) || can(USERS_IMPERSONATE)
      ? [{ value: "users", label: "Users" }]
      : []),
  ];

  return (
    <div>
      <PageHeader
        title="Billing control plane"
        description="Prices, plans, and who is on what. Changes here take effect without a deploy."
      />
      <Card padding="lg">
        <CardHeader
          title="Platform billing"
          subtitle="Visible only to platform administrators, not to workspace owners."
        />
        <Tabs items={tabs} value={tab} onChange={setTab} aria-label="Billing sections" />
        <div className={styles.panel} role="tabpanel">
          {tab === "rates" && <RateCards />}
          {tab === "plans" && <Plans />}
          {tab === "subs" && <Subscriptions />}
          {tab === "customers" && <CustomersTab />}
          {tab === "revenue" && <Revenue />}
          {tab === "flags" && can(PRICING_WRITE) && <FeatureFlags />}
          {tab === "payments" && can(PRICING_WRITE) && <PaymentsTab />}
          {tab === "access" && can(ADMINS_MANAGE) && <PlatformAdmins />}
          {tab === "keys" && can(PROVIDERS_MANAGE) && <ProviderKeysTab />}
          {tab === "features" && can(FEATURES_MANAGE) && <FeatureSwitchesTab />}
          {tab === "runtime" && can(PRICING_WRITE) && <RuntimeConfigTab />}
          {tab === "users" && (can(USERS_MANAGE) || can(USERS_IMPERSONATE)) && (
            <UserAdmin />
          )}
        </div>
      </Card>
    </div>
  );
}

export default AdminBillingPage;
