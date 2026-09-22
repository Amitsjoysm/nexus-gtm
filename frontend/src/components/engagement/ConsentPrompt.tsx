import { useState } from "react";
import { Button, Modal } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { TrainingConsentState } from "@/lib/types";
import styles from "./ConsentPrompt.module.css";

/**
 * The one-time question for workspaces created before the ledger existed (D24, spec §18.6).
 *
 * Shown to an owner or admin while the workspace has never decided; nothing is collected until then.
 * "Keep on" is the pre-selected choice, as it is at sign-up, and both buttons record a decision, so
 * the prompt never returns once answered. Closing it without choosing asks again next session.
 */
export function ConsentPrompt() {
  const api = useApiClient();
  const toast = useToast();
  const state = useApi<TrainingConsentState>((signal) => api.trainingConsent(signal), []);
  const [dismissed, setDismissed] = useState(false);
  const [saving, setSaving] = useState<"on" | "off" | null>(null);

  if (!state.data?.prompt || dismissed) return null;

  async function decide(status: "on" | "off") {
    setSaving(status);
    try {
      await api.setTrainingConsent(status);
      toast.success(
        status === "on" ? "Thanks" : "Switched off",
        status === "on"
          ? "Workspace data will help improve the AI. You can change this in Settings."
          : "Nothing will be collected. You can change this in Settings.",
      );
      setDismissed(true);
    } catch (err) {
      toast.error("Couldn't save your choice", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(null);
    }
  }

  return (
    <Modal
      open
      onClose={() => setDismissed(true)}
      title="Help improve the AI with this workspace's data?"
      description="The product can learn from what happens in this workspace: drafts and edits, replies and outcomes. Training copies have names, addresses and companies replaced."
      footer={
        <>
          <Button variant="ghost" onClick={() => decide("off")} loading={saving === "off"}
            disabled={saving !== null}>
            Turn off
          </Button>
          <Button onClick={() => decide("on")} loading={saving === "on"} disabled={saving !== null}
            autoFocus>
            Keep on
          </Button>
        </>
      }
    >
      <p className={styles.text}>
        <a href="/data-use" target="_blank" rel="noopener noreferrer">What is collected and how it is used</a>
      </p>
    </Modal>
  );
}
