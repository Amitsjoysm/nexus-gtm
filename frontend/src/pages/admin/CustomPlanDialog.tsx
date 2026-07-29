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

interface QuotaOverride {
  capability: string;
  quota: string;
}

/**
 * Build a negotiated, per-customer plan.
 *
 * Cloned from a base plan so a deal never starts from a blank slate, then only the terms that
 * were actually negotiated are overridden. The result is a real plan row, so the entitlement
 * engine and invoicing treat it exactly like a standard plan.
 */
export function CustomPlanDialog({ open, onClose, tenant, plans, onDone }: Props) {
  const api = useApiClient();
  const toast = useToast();
  const [basePlan, setBasePlan] = useState("growth");
  const [name, setName] = useState("");
  const [price, setPrice] = useState("2500");
  const [credits, setCredits] = useState("50000");
  const [seats, setSeats] = useState("");
  const [overrides, setOverrides] = useState<QuotaOverride[]>([]);
  const [saving, setSaving] = useState(false);

  const sellable = plans.filter((p) => p.plan_class !== "custom");

  function addOverride() {
    setOverrides((rows) => [...rows, { capability: "", quota: "" }]);
  }

  function setOverride(index: number, patch: Partial<QuotaOverride>) {
    setOverrides((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  function removeOverride(index: number) {
    setOverrides((rows) => rows.filter((_, i) => i !== index));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!tenant) return;
    setSaving(true);
    try {
      const entitlement_overrides: Record<string, { quota: number }> = {};
      for (const row of overrides) {
        const capability = row.capability.trim();
        if (!capability || row.quota.trim() === "") continue;
        entitlement_overrides[capability] = { quota: Number(row.quota) };
      }
      const result = (await api.createCustomPlan(tenant.tenant_id, {
        base_plan_id: basePlan,
        name: name.trim() || `${tenant.tenant_name} (custom)`,
        // Dollars in the form, integer cents on the wire — money is never a float here.
        base_price_cents: Math.round(Number(price) * 100),
        included_credits: Number(credits) || 0,
        max_seats: seats.trim() === "" ? null : Number(seats),
        entitlement_overrides,
        assign: true,
      })) as { plan_id?: string; provider?: { price_id?: string; error?: string } };

      if (result.provider?.error) {
        toast.error(
          "Plan saved, but not published to Stripe",
          "The deal exists and can be published again; the payment provider rejected the price.",
        );
      } else {
        toast.success(
          `${result.plan_id} assigned to ${tenant.tenant_name}`,
          result.provider?.price_id ? `Stripe price ${result.provider.price_id}` : undefined,
        );
      }
      onDone();
      onClose();
    } catch (err) {
      toast.error(
        "Couldn't create the custom plan",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tenant ? `Custom plan for ${tenant.tenant_name}` : "Custom plan"}
      description="Clone a plan, change only what was negotiated, and publish it to the payment provider."
    >
      <form onSubmit={submit} className={styles.form}>
        <Field label="Start from" hint="Entitlements are copied from this plan.">
          <Select
            value={basePlan}
            onChange={(e) => setBasePlan(e.target.value)}
            options={sellable.map((p) => ({
              value: p.id,
              label: `${p.name} — $${(p.base_price_cents / 100).toFixed(2)}/${p.interval}`,
            }))}
          />
        </Field>

        <Field label="Plan name" hint="Shown on the invoice.">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={tenant ? `${tenant.tenant_name} (custom)` : "Acme (custom)"}
          />
        </Field>

        <div className={styles.row}>
          <Field label="Price per month (USD)">
            <Input
              type="number"
              min="0"
              step="0.01"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              required
            />
          </Field>
          <Field label="Included credits" hint="1 credit = $0.01 of usage.">
            <Input
              type="number"
              min="0"
              value={credits}
              onChange={(e) => setCredits(e.target.value)}
            />
          </Field>
          <Field label="Seats" hint="Blank keeps the base plan's limit.">
            <Input
              type="number"
              min="0"
              value={seats}
              onChange={(e) => setSeats(e.target.value)}
              placeholder="unchanged"
            />
          </Field>
        </div>

        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Quota overrides</legend>
          <p className={styles.hint}>
            Only list what differs from the base plan. Everything else is inherited.
          </p>
          {overrides.map((row, i) => (
            <div key={i} className={styles.row}>
              <Field label="Capability">
                <Input
                  value={row.capability}
                  onChange={(e) => setOverride(i, { capability: e.target.value })}
                  placeholder="verify.email"
                />
              </Field>
              <Field label="Quota">
                <Input
                  type="number"
                  min="0"
                  value={row.quota}
                  onChange={(e) => setOverride(i, { quota: e.target.value })}
                  placeholder="250000"
                />
              </Field>
              <Button
                type="button"
                variant="ghost"
                onClick={() => removeOverride(i)}
                aria-label={`Remove override ${i + 1}`}
              >
                Remove
              </Button>
            </div>
          ))}
          <Button type="button" variant="secondary" onClick={addOverride}>
            Add a quota override
          </Button>
        </fieldset>

        <div className={styles.actions}>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" loading={saving} disabled={!tenant}>
            Create and assign
          </Button>
        </div>
      </form>
    </Modal>
  );
}
