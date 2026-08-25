import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Field, Input, Select, Skeleton, useToast } from "@/components/ui";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type {
  AdminPlan,
  AdminSubscriptionDetail,
  CustomerRow,
  CustomerUsage,
  SubscriptionPatch,
} from "@/lib/types";
import styles from "./CustomersTab.module.css";

/**
 * Find a workspace, see what it uses, and act on it.
 *
 * The question this answers first is "which workspace is this person in?", which had no surface at
 * all: the Subscriptions tab knew the plan, /billing/usage was tenant-scoped and answered only for
 * the caller, and credits were visible nowhere outside a dialog.
 *
 * **Credits belong to a workspace, not a person.** The ledger, quotas and the metering engine are
 * all tenant-scoped, so searching an email resolves through membership and the row reports which
 * address matched. The grant form says the workspace name back before it will submit.
 */

const STATUS_TONE: Record<string, "success" | "info" | "warning" | "danger" | "neutral"> = {
  active: "success",
  trialing: "info",
  past_due: "warning",
  suspended: "danger",
  canceled: "neutral",
  none: "neutral",
};

function statusLabel(status: string): string {
  return status === "none" ? "No subscription" : status.replace(/_/g, " ");
}

/** `2026-08-31T00:00:00Z` → `31 Aug 2026`. Null renders as an em-space so columns stay aligned. */
function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/** A datetime-local input wants `YYYY-MM-DDTHH:mm` and rejects anything else silently. */
function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function CustomersTab() {
  const api = useApiClient();
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<CustomerRow[] | null>(null);
  const [selected, setSelected] = useState<CustomerRow | null>(null);

  // Debounced so typing an email does not fire a cross-tenant aggregate per keystroke.
  useEffect(() => {
    const handle = window.setTimeout(() => {
      api
        .adminCustomers(query)
        .then(setRows)
        .catch(() => setRows([]));
    }, 250);
    return () => window.clearTimeout(handle);
  }, [api, query]);

  const columns: Column<CustomerRow>[] = [
    {
      key: "workspace",
      header: "Workspace",
      sortable: true,
      render: (r) => (
        <span className={styles.nameCell}>
          <span className={styles.name}>{r.workspace}</span>
          {/* Shown only when an email matched, so an operator can confirm they found the right
              person rather than a workspace containing a similar address. */}
          {r.matched_email && <span className={styles.matched}>{r.matched_email}</span>}
        </span>
      ),
    },
    { key: "plan_name", header: "Plan", sortable: true },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <Badge tone={STATUS_TONE[r.status] ?? "neutral"} dot>
          {statusLabel(r.status)}
        </Badge>
      ),
    },
    {
      key: "users",
      header: "Seats",
      align: "right",
      sortable: true,
      hideOnMobile: true,
      render: (r) => formatNumber(r.users),
    },
    {
      key: "requests_this_period",
      header: "Requests",
      align: "right",
      sortable: true,
      render: (r) => formatNumber(r.requests_this_period),
    },
    {
      key: "credits_balance",
      header: "Credits",
      align: "right",
      sortable: true,
      hideOnMobile: true,
      render: (r) => formatNumber(r.credits_balance),
    },
  ];

  return (
    <div className={styles.stack}>
      <div className={styles.search}>
        <Field
          label="Find a workspace"
          hint="By workspace name, or by the email of anyone in it."
        >
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="rep@acme.com or Acme Co"
            autoComplete="off"
          />
        </Field>
      </div>

      <DataTable
        columns={columns}
        rows={rows ?? []}
        getRowKey={(r) => r.tenant_id}
        loading={rows === null}
        minWidth={760}
        density="compact"
        caption="Workspaces on this platform"
        onRowClick={(r) => setSelected(selected?.tenant_id === r.tenant_id ? null : r)}
        empty={
          <p className={styles.empty}>
            {query
              ? `Nothing matches "${query}". Try a workspace name, or the full email address of a member.`
              : "No workspaces yet."}
          </p>
        }
      />

      {selected && (
        <CustomerDetail
          key={selected.tenant_id}
          row={selected}
          onClose={() => setSelected(null)}
          onChanged={() => {
            toast.success("Saved", `${selected.workspace} updated.`);
            api.adminCustomers(query).then(setRows).catch(() => undefined);
          }}
        />
      )}
    </div>
  );
}

/**
 * One workspace, opened inline rather than in a modal.
 *
 * There is too much here for a dialog (terms, per-capability usage, three action groups), and a
 * modal would also hide the table an operator is comparing against.
 */
function CustomerDetail({
  row,
  onClose,
  onChanged,
}: {
  row: CustomerRow;
  onClose: () => void;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [usage, setUsage] = useState<CustomerUsage | null>(null);
  const [sub, setSub] = useState<AdminSubscriptionDetail | null>(null);
  const [plans, setPlans] = useState<AdminPlan[]>([]);
  const [busy, setBusy] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  const load = useCallback(async () => {
    const [u, s, p] = await Promise.all([
      api.adminCustomerUsage(row.tenant_id),
      api.adminTenantSubscription(row.tenant_id),
      api.adminBillingPlans().catch(() => [] as AdminPlan[]),
    ]);
    setUsage(u);
    setSub(s.subscription);
    setPlans(p);
  }, [api, row.tenant_id]);

  useEffect(() => {
    load().catch(() => setUsage(null));
    // Move focus to the panel that just opened, so a keyboard user is not left on the table row.
    headingRef.current?.focus();
  }, [load]);

  function fail(title: string, err: unknown) {
    toast.error(title, err instanceof ApiError ? err.detail : "Please try again.");
  }

  async function act<T>(title: string, fn: () => Promise<T>) {
    setBusy(true);
    try {
      await fn();
      await load();
      onChanged();
    } catch (err) {
      fail(title, err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={styles.detail} aria-label={`${row.workspace} details`}>
      <header className={styles.detailHead}>
        <h3 className={styles.detailTitle} ref={headingRef} tabIndex={-1}>
          {row.workspace}
        </h3>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </header>

      {usage === null ? (
        <Skeleton width="100%" height={220} />
      ) : (
        <div className={styles.panels}>
          <Terms
            sub={sub}
            plans={plans}
            busy={busy}
            tenantId={row.tenant_id}
            onAct={act}
          />
          <Usage usage={usage} />
          <Credits tenantId={row.tenant_id} workspace={row.workspace} usage={usage} onAct={act} busy={busy} />
        </div>
      )}
    </section>
  );
}

function Terms({
  sub,
  plans,
  busy,
  tenantId,
  onAct,
}: {
  sub: AdminSubscriptionDetail | null;
  plans: AdminPlan[];
  busy: boolean;
  tenantId: string;
  onAct: <T>(title: string, fn: () => Promise<T>) => Promise<void>;
}) {
  const api = useApiClient();
  const [editing, setEditing] = useState(false);
  const [planId, setPlanId] = useState(sub?.plan_id ?? "");
  const [patch, setPatch] = useState<SubscriptionPatch>({});
  const [reason, setReason] = useState("");

  useEffect(() => {
    setPlanId(sub?.plan_id ?? "");
  }, [sub?.plan_id]);

  if (sub === null) {
    return (
      <section>
        <h4 className={styles.panelTitle}>Subscription</h4>
        <p className={styles.note}>
          This workspace has no subscription. Put it on a plan to start billing it.
        </p>
        <div className={styles.row}>
          <Field label="Plan">
            <Select
              value={planId}
              onChange={(e) => setPlanId(e.target.value)}
              options={[
                { value: "", label: "Choose a plan…" },
                ...plans.map((p) => ({ value: p.id, label: p.name })),
              ]}
            />
          </Field>
          <Button
            size="sm"
            disabled={!planId || busy}
            onClick={() => onAct("Couldn't set the plan", () =>
              api.setTenantSubscription(tenantId, planId))}
          >
            Start subscription
          </Button>
        </div>
      </section>
    );
  }

  return (
    <section>
      <h4 className={styles.panelTitle}>Subscription</h4>
      <dl className={styles.figures}>
        <div>
          <dt>Plan</dt>
          <dd>{sub.plan_name}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>
            <Badge tone={STATUS_TONE[sub.status] ?? "neutral"} dot>
              {statusLabel(sub.status)}
            </Badge>
          </dd>
        </div>
        <div>
          <dt>Renews</dt>
          <dd>{shortDate(sub.current_period_end)}</dd>
        </div>
        <div>
          <dt>Trial ends</dt>
          <dd>{shortDate(sub.trial_end)}</dd>
        </div>
        <div>
          <dt>Seats included</dt>
          <dd>{sub.seats_included == null ? "Unlimited" : formatNumber(sub.seats_included)}</dd>
        </div>
        <div>
          <dt>Stripe</dt>
          {/* An enterprise deal never had a provider object, and reconciliation skips it for that
              reason. Saying "not linked" beats an empty cell that reads as missing data. */}
          <dd className={styles.mono}>{sub.psp_subscription_id || "Not linked"}</dd>
        </div>
      </dl>

      {sub.cancel_at_period_end && (
        <p className={styles.warn}>
          Cancels on {shortDate(sub.current_period_end)}. Access continues until then.
        </p>
      )}

      <div className={styles.row}>
        <Field label="Move to plan">
          <Select
            value={planId}
            onChange={(e) => setPlanId(e.target.value)}
            options={plans.map((p) => ({ value: p.id, label: p.name }))}
          />
        </Field>
        <Button
          size="sm"
          disabled={busy || !planId || planId === sub.plan_id}
          onClick={() => onAct("Couldn't change the plan", () =>
            api.setTenantSubscription(tenantId, planId))}
        >
          Change plan
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setEditing((v) => !v)} disabled={busy}>
          {editing ? "Cancel edit" : "Edit terms"}
        </Button>
      </div>

      {editing && (
        <div className={styles.editor}>
          {/* Deliberately separate from "Change plan": a plan change runs proration, and a form
              that posted every field it had loaded would reprice a customer by accident. */}
          <div className={styles.row}>
            <Field label="Status">
              <Select
                value={patch.status ?? sub.status}
                onChange={(e) => setPatch({ ...patch, status: e.target.value })}
                options={["trialing", "active", "past_due", "suspended", "canceled"].map((s) => ({
                  value: s,
                  label: statusLabel(s),
                }))}
              />
            </Field>
            <Field label="Trial ends">
              <Input
                type="datetime-local"
                value={patch.trial_end ?? toLocalInput(sub.trial_end)}
                onChange={(e) => setPatch({ ...patch, trial_end: e.target.value })}
              />
            </Field>
            <Field label="Period ends">
              <Input
                type="datetime-local"
                value={patch.current_period_end ?? toLocalInput(sub.current_period_end)}
                onChange={(e) => setPatch({ ...patch, current_period_end: e.target.value })}
              />
            </Field>
            <Field label="Seats included" hint="Blank for unlimited.">
              <Input
                type="number"
                min={0}
                value={
                  patch.seats_included ?? (sub.seats_included == null ? "" : sub.seats_included)
                }
                onChange={(e) =>
                  setPatch({
                    ...patch,
                    seats_included: e.target.value === "" ? null : Number(e.target.value),
                  })
                }
              />
            </Field>
          </div>
          <Field label="Reason" hint="Recorded in the audit log against your name.">
            <Input value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          <div className={styles.actions}>
            <Button
              size="sm"
              disabled={busy || Object.keys(patch).length === 0}
              onClick={async () => {
                await onAct("Couldn't save the terms", () =>
                  api.patchTenantSubscription(tenantId, { ...patch, reason }));
                setPatch({});
                setEditing(false);
              }}
            >
              Save terms
            </Button>
          </div>
        </div>
      )}

      <div className={styles.actions}>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy || sub.cancel_at_period_end}
          onClick={() =>
            onAct("Couldn't cancel", () =>
              api.cancelTenantSubscription(tenantId, {
                at_period_end: true,
                reason: reason || "cancelled from the Control plane",
              }))
          }
        >
          Cancel at period end
        </Button>
        {/* Separate, and it asks: ending access the moment someone requests it takes back
            something they already paid for. */}
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => {
            if (
              !window.confirm(
                `End ${sub.plan_name} immediately? They paid through ${shortDate(sub.current_period_end)} and will lose access now.`,
              )
            ) {
              return;
            }
            onAct("Couldn't cancel", () =>
              api.cancelTenantSubscription(tenantId, {
                at_period_end: false,
                reason: reason || "immediate cancellation from the Control plane",
              }));
          }}
        >
          Cancel now
        </Button>
      </div>
    </section>
  );
}

function Usage({ usage }: { usage: CustomerUsage }) {
  const peak = Math.max(1, ...usage.capabilities.map((c) => c.used));
  return (
    <section>
      <h4 className={styles.panelTitle}>Used this period · {usage.period}</h4>
      {usage.capabilities.length === 0 ? (
        <p className={styles.note}>
          Nothing metered this period. {formatNumber(usage.requests_total)} requests all time.
        </p>
      ) : (
        <ul className={styles.usage}>
          {usage.capabilities.map((c) => (
            <li key={c.capability_id}>
              <span className={styles.usageName}>{c.name}</span>
              <span className={styles.usageBar} aria-hidden>
                <span style={{ inlineSize: `${(c.used / peak) * 100}%` }} />
              </span>
              <span className={styles.usageValue}>{formatNumber(c.used)}</span>
            </li>
          ))}
        </ul>
      )}
      <p className={styles.note}>
        {formatNumber(usage.requests_this_period)} requests this period,{" "}
        {formatNumber(usage.requests_total)} all time.
      </p>
    </section>
  );
}

function Credits({
  tenantId,
  workspace,
  usage,
  onAct,
  busy,
}: {
  tenantId: string;
  workspace: string;
  usage: CustomerUsage;
  onAct: <T>(title: string, fn: () => Promise<T>) => Promise<void>;
  busy: boolean;
}) {
  const api = useApiClient();
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");

  return (
    <section>
      <h4 className={styles.panelTitle}>Credits</h4>
      <dl className={styles.figures}>
        <div>
          <dt>Balance</dt>
          <dd>{formatNumber(usage.credits_balance)}</dd>
        </div>
      </dl>
      {/* Names the workspace, not the person searched for. Credits are tenant-scoped: everyone in
          this workspace spends from this balance, and an operator granting on behalf of one
          person should know that before they submit. */}
      <p className={styles.note}>
        Granted to <strong>{workspace}</strong> and spendable by everyone in it.
      </p>
      <div className={styles.row}>
        <Field label="Credits to grant">
          <Input
            type="number"
            min={1}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="500"
          />
        </Field>
        <Field label="Reason" hint="Appears in the customer's own ledger.">
          <Input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="goodwill — failed enrichment run"
          />
        </Field>
        <Button
          size="sm"
          disabled={busy || !amount || Number(amount) <= 0 || !reason.trim()}
          onClick={async () => {
            await onAct("Couldn't grant the credits", () =>
              api.grantTenantCredits(tenantId, {
                amount: Number(amount),
                reason: reason.trim(),
                // Derived from the grant itself, not random: a double-clicked button reaches the
                // same key and the server refuses the second one instead of minting credits twice.
                idempotency_key: `admin:${tenantId}:${amount}:${reason.trim()}`,
              }));
            setAmount("");
            setReason("");
          }}
        >
          Grant credits
        </Button>
      </div>
    </section>
  );
}
