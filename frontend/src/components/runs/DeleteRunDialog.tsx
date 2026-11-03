import { useState } from "react";
import { Button, Modal, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import styles from "./DeleteRunDialog.module.css";

export interface DeleteRunDialogProps {
  /** The run to delete; `null` keeps the dialog closed. */
  runId: string | null;
  /** What the run was, for the question ("Delete Research account?"). */
  label?: string;
  /** A run still working is stopped first, and the dialog says so. */
  active?: boolean;
  onClose: () => void;
  onDeleted: (runId: string) => void;
}

/**
 * Confirm deleting one AI run. Shared by AI Runs, the run console and the Orchestrator's run card,
 * so the three say the same thing about what a delete keeps.
 *
 * The server's delete is soft: the run leaves every screen, and its usage stays on the credit
 * report, which is why the dialog says "credits it used stay" rather than implying a refund.
 */
export function DeleteRunDialog({ runId, label, active, onClose, onDeleted }: DeleteRunDialogProps) {
  const api = useApiClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function confirm() {
    if (!runId) return;
    setBusy(true);
    try {
      await api.deleteRun(runId);
      toast.success("Run deleted", "It no longer appears in AI Runs.");
      onDeleted(runId);
      onClose();
    } catch (err) {
      toast.error("Couldn't delete the run", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={runId !== null}
      onClose={onClose}
      size="sm"
      title={label ? `Delete ${label}?` : "Delete this run?"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" loading={busy} onClick={() => void confirm()}>
            Delete run
          </Button>
        </>
      }
    >
      <div className={styles.body}>
        <p>
          It leaves AI Runs and its conversation. The accounts and contacts it added stay in your
          workspace, and the credits it used stay on your usage report.
        </p>
        {active && (
          <p className={styles.note}>
            It is still working, so it is stopped first. A send waiting for approval is rejected
            and nothing more goes out.
          </p>
        )}
      </div>
    </Modal>
  );
}
