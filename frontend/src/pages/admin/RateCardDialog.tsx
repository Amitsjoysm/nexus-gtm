import { useEffect, useState } from "react";
import { Button, Field, Input, Modal, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { AdminRateCard } from "@/lib/types";
import styles from "./AdminForms.module.css";

const MARGIN_FLOOR = 0.5;

interface Props {
  open: boolean;
  onClose: () => void;
  card: AdminRateCard | null;
  onDone: () => void;
}

/** 1 credit = $0.01 list. */
function creditsToUsd(credits: number): string {
  return `$${(credits / 100).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}`;
}

/**
 * Reprice one capability.
 *
 * The API for this has existed since M6 and nothing ever called it — the console showed the price
 * and gave you no way to change it, which is the dead-config shape the billing platform was built
 * to escape. "Pricing belongs to Admin, not to a redeploy" was only half true while the only way
 * to exercise it was curl.
 *
 * The margin is computed live against the stored COGS, because the server refuses a below-floor
 * price with a 422 and finding that out after submitting is a worse experience than being told
 * while typing. The guardrail is still enforced server-side; this only surfaces it earlier.
 */
export function RateCardDialog({ open, onClose, card, onDone }: Props) {
  const api = useApiClient();
  const toast = useToast();
  const [credits, setCredits] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCredits(card ? String(card.credits_per_unit) : "");
    setReason("");
  }, [card]);

  const value = Number(credits);
  const valid = credits.trim() !== "" && Number.isFinite(value) && value >= 0;
  const cogs = card?.unit_cost_usd ?? 0;
  // Same arithmetic as nexus/billing/rates.py: (revenue - cost) / revenue.
  const revenue = value / 100;
  const margin = valid && revenue > 0 ? (revenue - cogs) / revenue : 0;
  const belowFloor = valid && value > 0 && margin < MARGIN_FLOOR;
  // A zero price is free-of-charge, not a margin violation, so it is allowed without an exception.
  const needsException = belowFloor;
  const canSave = valid && (!needsException || reason.trim().length > 0);

  async function save() {
    if (!card || !canSave) return;
    setBusy(true);
    try {
      await api.upsertRateCard(card.capability_id, {
        credits_per_unit: value,
        tiers: card.tiers ?? [],
        active: true,
        margin_exception: needsException,
        margin_exception_reason: reason.trim(),
      });
      toast.success(
        `${card.name} repriced`,
        `${value} credits (${creditsToUsd(value)}) per ${card.unit}. Effective immediately.`,
      );
      onDone();
      onClose();
    } catch (err) {
      // The server enforces the floor regardless; surface its reason rather than a generic failure.
      toast.error(
        "Couldn't reprice",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={card ? card.name : "Rate card"}
      description="Sets what this capability costs a customer. Takes effect immediately, with no deploy."
    >
      <div className={styles.form}>
        <Field
          label={`Credits per ${card?.unit ?? "unit"}`}
          hint={
            valid
              ? `${creditsToUsd(value)} list price · COGS $${cogs.toFixed(4)}`
              : "1 credit = $0.01."
          }
          error={!valid && credits !== "" ? "Enter a number of 0 or more." : undefined}
        >
          <Input
            type="number"
            min="0"
            step="0.5"
            value={credits}
            onChange={(e) => setCredits(e.target.value)}
            placeholder="3"
          />
        </Field>

        {valid && value > 0 && (
          <p className={styles.marginPreview}>
            <span>Gross margin</span>
            <span className={belowFloor ? styles.marginBad : styles.marginOk}>
              {(margin * 100).toFixed(0)}%
            </span>
            {belowFloor && (
              <span className={styles.hint}>
                below the {MARGIN_FLOOR * 100}% floor
              </span>
            )}
          </p>
        )}

        {needsException && (
          <Field
            label="Margin exception reason"
            hint="Required below the floor. Recorded in the audit log with the before and after price."
            error={reason.trim() ? undefined : "Finance must record why this price is below cost."}
          >
            <Input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Loss-leader for Q4 enterprise pilot, approved by finance"
            />
          </Field>
        )}

        <div className={styles.actions}>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!canSave}>
            Save price
          </Button>
        </div>
      </div>
    </Modal>
  );
}
