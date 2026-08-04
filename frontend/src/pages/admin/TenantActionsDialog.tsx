import { useEffect, useState } from "react";
import { Button, Field, Input, Modal, Select, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { usePlatformCan, usePlatformIdentity } from "@/app/RequirePlatformAdmin";
import { CREDITS_GRANT, CREDITS_GRANT_CAPPED, SUBSCRIPTIONS_WRITE } from "@/lib/permissions";
import { ApiError } from "@/lib/api";
import type { AdminPlan, AdminSubscription, ProrationPreview } from "@/lib/types";
import styles from "./AdminForms.module.css";

function money(cents: number): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
  }).format(Math.abs(cents) / 100);
}

/**
 * What the plan change will actually cost, before the admin commits to it.
 *
 * Both sides are shown even when one is zero. An admin who sees only "charge $47.00" cannot tell
 * whether the customer was credited for the days they already paid, and that is the question the
 * customer will ask when the invoice arrives.
 */
function ProrationSummary({ preview }: { preview: ProrationPreview }) {
  if (preview.days_remaining === 0) {
    return (
      <p className={styles.hint}>
        No days remain in this period, so the change is not prorated. The new plan is charged in
        full next period.
      </p>
    );
  }
  const owes = preview.net_cents > 0;
  return (
    <div className={styles.proration}>
      <dl className={styles.prorationRows}>
        <div className={styles.prorationRow}>
          <dt>Credit for unused days</dt>
          <dd className={styles.mono}>−{money(preview.credit_cents)}</dd>
        </div>
        <div className={styles.prorationRow}>
          <dt>New plan, {preview.days_remaining} remaining days</dt>
          <dd className={styles.mono}>{money(preview.charge_cents)}</dd>
        </div>
        <div className={styles.prorationNet}>
          <dt>{owes ? "Added to this period" : "Credited to this period"}</dt>
          <dd className={styles.mono}>
            {owes ? "" : "−"}
            {money(preview.net_cents)}
          </dd>
        </div>
      </dl>
      <p className={styles.hint}>
        {preview.days_remaining} of {preview.days_in_period} days remain. This lands on the current
        period&rsquo;s invoice, not as a separate charge.
      </p>
    </div>
  );
}

interface Props {
  open: boolean;
  onClose: () => void;
  tenant: AdminSubscription | null;
  plans: AdminPlan[];
  onDone: () => void;
}

/** Move a workspace between plans, or credit its account. */
export function TenantActionsDialog({ open, onClose, tenant, plans, onDone }: Props) {
  const api = useApiClient();
  const toast = useToast();
  const can = usePlatformCan();
  const identity = usePlatformIdentity();
  const [planId, setPlanId] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [pauseReason, setPauseReason] = useState("");
  const [busy, setBusy] = useState<"plan" | "credits" | "pause" | null>(null);
  const [preview, setPreview] = useState<ProrationPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const canChangePlan = can(SUBSCRIPTIONS_WRITE);
  const canGrant = can(CREDITS_GRANT) || can(CREDITS_GRANT_CAPPED);
  const paused = tenant?.status === "suspended";

  // Fetch the money as soon as a plan is picked. Aborted on change so a slow response for an
  // abandoned selection cannot land after a newer one and show the wrong number.
  useEffect(() => {
    if (!tenant || !planId || !canChangePlan) {
      setPreview(null);
      return;
    }
    const controller = new AbortController();
    setPreviewing(true);
    api
      .prorationPreview(tenant.tenant_id, planId, controller.signal)
      .then(setPreview)
      .catch(() => setPreview(null))
      .finally(() => {
        if (!controller.signal.aborted) setPreviewing(false);
      });
    return () => controller.abort();
  }, [api, tenant, planId, canChangePlan]);
  // A ceiling only applies to someone who holds *only* the capped permission. The number comes
  // from the server so the console never contradicts the limit actually enforced.
  const cap = can(CREDITS_GRANT) ? null : identity?.credit_grant_cap ?? null;
  const overCap = cap != null && Number(amount) > cap;

  async function changePlan() {
    if (!tenant || !planId) return;
    setBusy("plan");
    try {
      await api.setTenantSubscription(tenant.tenant_id, planId);
      toast.success(`${tenant.tenant_name} moved to ${planId}`);
      onDone();
      onClose();
    } catch (err) {
      toast.error(
        "Couldn't change the plan",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function togglePause() {
    if (!tenant) return;
    setBusy("pause");
    try {
      if (paused) {
        const res = await api.resumeTenantSubscription(tenant.tenant_id, pauseReason.trim());
        toast.success(
          `${tenant.tenant_name} resumed`,
          res.days_returned
            ? `The period end moved out by ${res.days_returned} paused day${
                res.days_returned === 1 ? "" : "s"
              }.`
            : undefined,
        );
      } else {
        await api.pauseTenantSubscription(tenant.tenant_id, pauseReason.trim());
        toast.success(
          `${tenant.tenant_name} paused`,
          "Billing and access are suspended. The plan and history are kept.",
        );
      }
      setPauseReason("");
      onDone();
      onClose();
    } catch (err) {
      // The server refuses a pause on a past_due subscription and says why. Show its reason
      // rather than a generic failure: "settle the outstanding balance" is actionable.
      toast.error(
        paused ? "Couldn't resume" : "Couldn't pause",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function grantCredits() {
    if (!tenant || !amount) return;
    setBusy("credits");
    try {
      const res = await api.grantTenantCredits(tenant.tenant_id, {
        amount: Number(amount),
        reason: reason.trim() || "Admin adjustment",
        // Derived, not random: a double-clicked button reuses the same key and the server
        // refuses the second grant instead of minting credits twice.
        idempotency_key: `admin:${tenant.tenant_id}:${amount}:${reason.trim()}`,
      });
      toast.success(
        res.applied ? `Granted ${amount} credits` : "Already applied",
        `Balance is now ${res.balance}`,
      );
      onDone();
      onClose();
    } catch (err) {
      toast.error(
        "Couldn't grant credits",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tenant ? tenant.tenant_name : "Workspace"}
      description={
        canChangePlan
          ? "Change the plan or adjust the credit balance for this workspace."
          : "Adjust the credit balance for this workspace."
      }
    >
      <div className={styles.form}>
        {canChangePlan && (
          <>
            <Field
              label="Move to plan"
              hint="Takes effect immediately and is prorated to the day."
            >
              <Select
                value={planId}
                onChange={(e) => setPlanId(e.target.value)}
                placeholder="Select a plan…"
                options={plans.map((p) => ({
                  value: p.id,
                  label: `${p.name} — $${(p.base_price_cents / 100).toFixed(2)}/${p.interval}`,
                }))}
              />
            </Field>
            {previewing && <p className={styles.hint}>Working out the proration…</p>}
            {!previewing && preview && <ProrationSummary preview={preview} />}
            <div className={styles.actions}>
              <Button onClick={changePlan} loading={busy === "plan"} disabled={!planId}>
                Change plan
              </Button>
            </div>
          </>
        )}

        {canChangePlan && (
          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>{paused ? "Resume" : "Pause"}</legend>
            <p className={styles.hint}>
              {paused
                ? "Restores access and pushes the period end out by however long the pause lasted, so the customer is not billed for paused days."
                : "Suspends billing and access while keeping the plan, its terms and the workspace history. A workspace with an unpaid balance cannot be paused."}
            </p>
            <Field
              label="Reason"
              hint="Recorded in the audit log. A pause nobody can explain later is worse than none."
            >
              <Input
                value={pauseReason}
                onChange={(e) => setPauseReason(e.target.value)}
                placeholder={paused ? "Customer returning from hiatus" : "Customer on hold until Q4"}
              />
            </Field>
            <div className={styles.actions}>
              <Button
                variant={paused ? "primary" : "secondary"}
                onClick={togglePause}
                loading={busy === "pause"}
              >
                {paused ? "Resume subscription" : "Pause subscription"}
              </Button>
            </div>
          </fieldset>
        )}

        {canGrant && (
          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>Grant credits</legend>
            <div className={styles.row}>
              <Field
                label="Credits"
                hint={
                  cap == null
                    ? "1 credit = $0.01."
                    : `1 credit = $0.01. Your limit is ${cap.toLocaleString()} per grant.`
                }
                error={overCap ? `Grants above ${cap?.toLocaleString()} need finance.` : undefined}
              >
                <Input
                  type="number"
                  min="1"
                  max={cap ?? undefined}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder="500"
                />
              </Field>
              <Field label="Reason" hint="Appears in the customer's ledger.">
                <Input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Goodwill adjustment"
                />
              </Field>
            </div>
            <div className={styles.actions}>
              <Button
                variant="secondary"
                onClick={grantCredits}
                loading={busy === "credits"}
                disabled={!amount || overCap}
              >
                Grant credits
              </Button>
            </div>
          </fieldset>
        )}
      </div>
    </Modal>
  );
}
