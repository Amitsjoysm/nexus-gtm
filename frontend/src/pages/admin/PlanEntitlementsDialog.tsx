import { useEffect, useState } from "react";
import { Badge, Button, Field, Input, Modal, Select, Skeleton, useToast } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { AdminPlan, PlanEntitlement } from "@/lib/types";
import styles from "./AdminForms.module.css";

const MODES = ["enabled", "metered", "unlimited", "disabled", "shadow", "enterprise"];

interface Props {
  open: boolean;
  onClose: () => void;
  plan: AdminPlan | null;
}

/**
 * Set what a plan includes, per capability.
 *
 * This is the other half of "pricing belongs to Admin, not to a redeploy": the rate card says what
 * a unit costs, this says how many of them a plan includes before overage. The API has existed
 * since M6 with no way to reach it.
 *
 * The list shows EVERY capability, not just configured ones. An unconfigured capability falls
 * through to the catalog default — which is easy to forget and impossible to notice from a list
 * that omits it, and is exactly the state an operator most needs to see.
 */
export function PlanEntitlementsDialog({ open, onClose, plan }: Props) {
  const api = useApiClient();
  const toast = useToast();
  const [editing, setEditing] = useState<PlanEntitlement | null>(null);
  const [mode, setMode] = useState("metered");
  const [quota, setQuota] = useState("");
  const [busy, setBusy] = useState(false);

  const rows = useApi<PlanEntitlement[]>(
    (signal) => (plan ? api.planEntitlements(plan.id, signal) : Promise.resolve([])),
    [plan?.id, open],
  );

  useEffect(() => {
    if (editing) {
      setMode(editing.mode || editing.default_mode || "metered");
      setQuota(editing.quota == null ? "" : String(editing.quota));
    }
  }, [editing]);

  async function save() {
    if (!plan || !editing) return;
    setBusy(true);
    try {
      await api.upsertEntitlement(plan.id, editing.capability_id, {
        mode,
        // Blank means unlimited, which is a real and different answer from zero. Sending 0 for an
        // empty box would silently disable the capability for everyone on the plan.
        quota: quota.trim() === "" ? null : Number(quota),
        soft_limit_pct: editing.soft_limit_pct ?? 80,
        overage_price_credits: editing.overage_price_credits,
        feature_flag: editing.feature_flag,
      });
      toast.success(
        `${editing.name} updated`,
        `${plan.name}: ${mode}${quota.trim() ? `, ${quota} ${editing.unit}s` : ", unlimited"}.`,
      );
      setEditing(null);
      rows.refetch();
    } catch (err) {
      toast.error("Couldn't save", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<PlanEntitlement>[] = [
    {
      key: "name",
      header: "Capability",
      render: (r) => (
        <div className={styles.adminMeta}>
          <span>{r.name}</span>
          <span className={styles.adminSub}>{r.capability_id}</span>
        </div>
      ),
      sortable: true,
      sortValue: (r) => r.name,
    },
    {
      key: "mode",
      header: "Mode",
      width: "130px",
      render: (r) =>
        r.configured ? (
          <Badge tone={r.mode === "disabled" ? "neutral" : "success"} dot>
            {r.mode}
          </Badge>
        ) : (
          // Naming the inherited value, not leaving a blank: the default IS the behaviour.
          <span className={styles.adminSub}>default · {r.default_mode}</span>
        ),
    },
    {
      key: "quota",
      header: "Included",
      width: "120px",
      align: "right",
      render: (r) =>
        r.quota == null ? (
          <span className={styles.adminSub}>unlimited</span>
        ) : (
          <span className={styles.mono}>{r.quota.toLocaleString()}</span>
        ),
    },
    {
      key: "actions",
      header: "",
      width: "100px",
      render: (r) => (
        <Button variant="secondary" onClick={() => setEditing(r)}>
          Edit
        </Button>
      ),
    },
  ];

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={plan ? `${plan.name} — what's included` : "Plan"}
      description="How much of each capability this plan includes before overage. Effective immediately."
    >
      {editing ? (
        <div className={styles.form}>
          <p className={styles.hint}>
            <strong>{editing.name}</strong> · {editing.capability_id}
          </p>
          <div className={styles.row}>
            <Field label="Mode" hint="disabled blocks it; metered counts and charges overage.">
              <Select
                value={mode}
                onChange={(e) => setMode(e.target.value)}
                options={MODES.map((m) => ({ value: m, label: m }))}
              />
            </Field>
            <Field
              label={`Included ${editing.unit}s`}
              hint="Leave blank for unlimited. Zero disables it for everyone on this plan."
            >
              <Input
                type="number"
                min="0"
                value={quota}
                onChange={(e) => setQuota(e.target.value)}
                placeholder="unlimited"
              />
            </Field>
          </div>
          <div className={styles.actions}>
            <Button variant="secondary" onClick={() => setEditing(null)}>
              Back
            </Button>
            <Button onClick={save} loading={busy}>
              Save
            </Button>
          </div>
        </div>
      ) : (
        <DataState
          state={rows}
          errorTitle="Couldn't load entitlements"
          skeleton={<Skeleton width="100%" height={220} />}
        >
          {(list) => (
            <DataTable
              columns={columns}
              rows={list}
              getRowKey={(r) => r.capability_id}
              caption="Plan entitlements"
              minWidth={620}
            />
          )}
        </DataState>
      )}
    </Modal>
  );
}
