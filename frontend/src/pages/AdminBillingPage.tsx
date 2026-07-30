import { useState } from "react";
import { Button } from "@/components/ui";
import { CustomPlanDialog } from "./admin/CustomPlanDialog";
import { TenantActionsDialog } from "./admin/TenantActionsDialog";
import { PlatformAdmins } from "./admin/PlatformAdmins";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Card, CardHeader, EmptyState, Skeleton, Tabs } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { usePlatformCan } from "@/app/RequirePlatformAdmin";
import { ADMINS_MANAGE, SUBSCRIPTIONS_WRITE } from "@/lib/permissions";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import type { AdminPlan, AdminRateCard, AdminSubscription } from "@/lib/types";
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
  const rates = useApi<AdminRateCard[]>((signal) => api.adminBillingRates(signal), []);

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
              minWidth={760}
            />
          </>
        );
      }}
    </DataState>
  );
}

function Plans() {
  const api = useApiClient();
  const plans = useApi<AdminPlan[]>((signal) => api.adminBillingPlans(signal), []);

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
        <DataTable
          columns={columns}
          rows={rows}
          getRowKey={(r) => r.id}
          caption="Plans"
          minWidth={760}
        />
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
                  : "neutral"
          }
        >
          {r.status}
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

export function AdminBillingPage() {
  const [tab, setTab] = useState("rates");
  const can = usePlatformCan();
  // Access is where power is granted, so it is visible only to admins who can grant it. The other
  // three tabs are reads, which every platform role holds.
  const tabs = [
    { value: "rates", label: "Rate cards" },
    { value: "plans", label: "Plans" },
    { value: "subs", label: "Subscriptions" },
    ...(can(ADMINS_MANAGE) ? [{ value: "access", label: "Access" }] : []),
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
          {tab === "access" && can(ADMINS_MANAGE) && <PlatformAdmins />}
        </div>
      </Card>
    </div>
  );
}

export default AdminBillingPage;
