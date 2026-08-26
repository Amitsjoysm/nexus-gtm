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
 * The margin is computed live against the COGS, because the server refuses a below-floor price
 * with a 422 and finding that out after submitting is worse than being told while typing. The
 * guardrail is still enforced server-side; this only surfaces it earlier.
 *
 * **Cost is editable here too, and that is not symmetry for its own sake.** Prices could be changed
 * without a deploy and costs could not, so `validate_rate` went on comparing every price against a
 * stored cost that had stopped being true — `search.web` carried $0.004 while we were paying Exa
 * $0.007, and sat at 30% margin with nothing complaining. Price and cost are the two halves of one
 * margin, and an operator recording a provider price rise almost always wants to reprice in the
 * same sitting.
 */
export function RateCardDialog({ open, onClose, card, onDone }: Props) {
  const api = useApiClient();
  const toast = useToast();
  const [credits, setCredits] = useState("");
  const [cost, setCost] = useState("");
  const [costSource, setCostSource] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCredits(card ? String(card.credits_per_unit) : "");
    setCost(card ? String(card.unit_cost_usd) : "");
    setCostSource("");
    setReason("");
  }, [card]);

  const value = Number(credits);
  const valid = credits.trim() !== "" && Number.isFinite(value) && value >= 0;
  const costValue = Number(cost);
  const costValid = cost.trim() !== "" && Number.isFinite(costValue) && costValue >= 0;
  const costChanged = costValid && costValue !== (card?.unit_cost_usd ?? 0);
  // Margin previews against the cost being TYPED, not the stored one — otherwise an operator
  // recording a price rise would watch a healthy margin right up until they saved.
  const cogs = costValid ? costValue : (card?.unit_cost_usd ?? 0);
  // Same arithmetic as nexus/billing/rates.py: (revenue - cost) / revenue.
  const revenue = value / 100;
  const margin = valid && revenue > 0 ? (revenue - cogs) / revenue : 0;
  const belowFloor = valid && value > 0 && margin < MARGIN_FLOOR;
  // A zero price is free-of-charge, not a margin violation, so it is allowed without an exception.
  const needsException = belowFloor;
  const canSave = valid && costValid && (!needsException || reason.trim().length > 0);

  async function save() {
    if (!card || !canSave) return;
    setBusy(true);
    try {
      // Cost FIRST, deliberately. Recording a cost is never refused; repricing below the floor is.
      // Writing cost first means the true number is stored even if the new price is then rejected —
      // the alternative leaves the system believing a stale cost, which is the exact failure this
      // field exists to end.
      if (costChanged) {
        const result = await api.upsertCostRate(card.capability_id, {
          unit_cost_usd: costValue,
          source: costSource.trim(),
        });
        // The response covers the WHOLE catalog, because one provider price change can move
        // several capabilities that share the input. Surfaced as a warning rather than swallowed:
        // an operator who only hears about the row they edited will not go looking for the others.
        const others = result.below_floor.filter((b) => b.capability_id !== card.capability_id);
        if (others.length > 0) {
          toast.error(
            `${others.length} other ${others.length === 1 ? "capability is" : "capabilities are"} now below the floor`,
            others.slice(0, 3).map((b) =>
              `${b.capability_id} needs ${b.credits_to_clear_floor} credits`).join("; "),
          );
        }
      }
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

        <Field
          label={`Our cost per ${card?.unit ?? "unit"}`}
          hint="What the provider charges us. Recording it is never refused, whatever it does to the margin."
          error={!costValid && cost !== "" ? "Enter a cost of 0 or more." : undefined}
        >
          <Input
            type="number"
            min="0"
            step="0.0001"
            value={cost}
            onChange={(e) => setCost(e.target.value)}
            placeholder="0.0070"
          />
        </Field>

        {costChanged && (
          <Field
            label="Where this cost came from"
            hint="An invoice line, a published price, a measured run. The cost is what the margin floor trusts, so this is the first thing anyone will ask."
          >
            <Input
              value={costSource}
              onChange={(e) => setCostSource(e.target.value)}
              placeholder="Exa list price, Aug 2026: $7 per 1,000"
            />
          </Field>
        )}

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
