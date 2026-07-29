import { useState } from "react";
import { Button, Field, Input, Modal, Select, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { AdminPlan, AdminSubscription } from "@/lib/types";
import styles from "./AdminForms.module.css";

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
  const [planId, setPlanId] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"plan" | "credits" | null>(null);

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
      description="Change the plan or adjust the credit balance for this workspace."
    >
      <div className={styles.form}>
        <Field label="Move to plan" hint="Takes effect immediately.">
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
        <div className={styles.actions}>
          <Button onClick={changePlan} loading={busy === "plan"} disabled={!planId}>
            Change plan
          </Button>
        </div>

        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Grant credits</legend>
          <div className={styles.row}>
            <Field label="Credits" hint="1 credit = $0.01.">
              <Input
                type="number"
                min="1"
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
              disabled={!amount}
            >
              Grant credits
            </Button>
          </div>
        </fieldset>
      </div>
    </Modal>
  );
}
