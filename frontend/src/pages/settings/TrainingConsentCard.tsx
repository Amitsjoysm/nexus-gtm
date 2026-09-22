import { useState } from "react";
import { Badge, Button, Card, CardHeader, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { TrainingConsentState } from "@/lib/types";
import styles from "./TrainingConsentCard.module.css";

/** Settings: switch the training & insights ledger on or off for this workspace (D24). */
export function TrainingConsentCard() {
  const api = useApiClient();
  const toast = useToast();
  const state = useApi<TrainingConsentState>((signal) => api.trainingConsent(signal), []);
  const [saving, setSaving] = useState(false);

  async function set(status: "on" | "off") {
    setSaving(true);
    try {
      const next = await api.setTrainingConsent(status);
      state.setData(next);
      toast.success(
        status === "on" ? "Switched on" : "Switched off",
        status === "on"
          ? "New activity will be collected from now on."
          : "Collection stopped and collected data is being deleted.",
      );
    } catch (err) {
      toast.error("Couldn't change it", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card padding="lg">
      <CardHeader
        title="Help improve the AI"
        subtitle="Let workspace activity improve drafting, reply reading and prospect timing advice."
      />
      <DataState state={state} errorTitle="Couldn't load this setting"
        skeleton={<Skeleton width="100%" height={48} />}>
        {(s) => (
          <div className={styles.row}>
            <div className={styles.status}>
              <Badge tone={s.status === "on" ? "success" : s.status === "off" ? "neutral" : "warning"} dot>
                {s.status === "on" ? "On" : s.status === "off" ? "Off" : "Not decided yet"}
              </Badge>
              <a href="/data-use" target="_blank" rel="noopener noreferrer">What is collected</a>
            </div>
            {s.can_decide && (
              s.status === "on" ? (
                <Button variant="secondary" loading={saving} onClick={() => set("off")}>
                  Turn off and delete
                </Button>
              ) : (
                <Button loading={saving} onClick={() => set("on")}>Turn on</Button>
              )
            )}
          </div>
        )}
      </DataState>
    </Card>
  );
}
