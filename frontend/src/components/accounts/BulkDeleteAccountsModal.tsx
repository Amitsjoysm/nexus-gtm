import { useEffect, useMemo, useState } from "react";
import { Button, Field, Icons, Modal, Skeleton, Textarea, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { AccountBulkDeletePreview, AccountBulkDeleteResult } from "@/lib/types";
import styles from "./BulkDeleteAccountsModal.module.css";

const plural = (n: number, one: string, many = `${one}s`) =>
  `${formatNumber(n)} ${n === 1 ? one : many}`;

export interface BulkDeleteAccountsModalProps {
  open: boolean;
  /** The ticked accounts. */
  ids: string[];
  onClose: () => void;
  /** After the delete, with the ids that were actually sent (any left out are not among them). */
  onDeleted: (result: AccountBulkDeleteResult, ids: string[]) => void;
}

/**
 * Confirm deleting accounts for good, with their contacts (SDR round 2, 5.5).
 *
 * The count comes from the server's preview, not the selection, so the question names what will
 * really go: the accounts, their contacts, and which ones a running campaign still holds (the
 * server refuses those; "Leave them out" deletes the rest). The two options are this workspace's
 * own: suppressing the domains for its discovery, and flagging the shared record for review, which
 * never changes what other workspaces see.
 */
export function BulkDeleteAccountsModal({ open, ids, onClose, onDeleted }: BulkDeleteAccountsModalProps) {
  const api = useApiClient();
  const toast = useToast();

  const [left, setLeft] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<AccountBulkDeletePreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [suppress, setSuppress] = useState(false);
  const [report, setReport] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const chosen = useMemo(() => ids.filter((id) => !left.has(id)), [ids, left]);

  useEffect(() => {
    if (open) return;
    setLeft(new Set());
    setPreview(null);
    setPreviewError("");
    setSuppress(false);
    setReport(false);
    setReason("");
  }, [open]);

  useEffect(() => {
    if (!open || chosen.length === 0) return;
    const ctrl = new AbortController();
    setPreview(null);
    setPreviewError("");
    api
      .previewBulkDeleteAccounts(chosen, ctrl.signal)
      .then(setPreview)
      .catch((err) => {
        if (!ctrl.signal.aborted)
          setPreviewError(err instanceof ApiError ? err.detail : "Couldn't check these accounts.");
      });
    return () => ctrl.abort();
  }, [api, open, chosen]);

  const blocked = preview?.in_running_campaigns ?? [];
  const reasonMissing = report && !reason.trim();
  const ready = !!preview && preview.accounts > 0 && blocked.length === 0 && !reasonMissing;

  async function confirm() {
    if (!ready) return;
    setBusy(true);
    try {
      const res = await api.bulkDeleteAccounts({
        ids: chosen,
        suppress,
        report_wrong: report,
        reason: report ? reason.trim() : "",
      });
      const parts = [
        res.contacts_deleted ? `${plural(res.contacts_deleted, "contact")} went with them.` : "",
        res.suppressed ? "They won't be suggested here again." : "",
        res.reported ? `${plural(res.reported, "company record")} flagged for review.` : "",
      ].filter(Boolean);
      toast.success(`${plural(res.deleted, "account")} deleted`, parts.join(" ") || undefined);
      onDeleted(res, chosen);
      onClose();
    } catch (err) {
      toast.error("Nothing was deleted", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusy(false);
    }
  }

  const count = preview?.accounts ?? chosen.length;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Delete ${plural(count, "account")} for good?`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" loading={busy} disabled={!ready} onClick={() => void confirm()}>
            Delete {plural(count, "account")}
          </Button>
        </>
      }
    >
      <div className={styles.body}>
        {previewError ? (
          <p className={styles.error} role="alert">
            {previewError}
          </p>
        ) : !preview ? (
          <div className={styles.loading} aria-label="Checking the accounts">
            <Skeleton width="90%" height={14} />
            <Skeleton width="70%" height={14} />
          </div>
        ) : (
          <>
            <p className={styles.lead}>
              {preview.contacts > 0
                ? `Their ${plural(preview.contacts, "contact")} go too, `
                : "They go "}
              with their signals, tasks, list places and call history. This can&rsquo;t be undone.
            </p>
            {preview.with_shared_record > 0 && (
              <p className={styles.kept}>
                <Icons.ShieldCheckIcon aria-hidden="true" />
                <span>
                  The company records stay in the shared database, so you can add them back later
                  and other workspaces keep them.
                </span>
              </p>
            )}

            {blocked.length > 0 && (
              <div className={styles.blocked} role="alert">
                <p className={styles.blockedTitle}>
                  {plural(blocked.length, "account is", "accounts are")} in a running campaign
                </p>
                <ul className={styles.blockedList}>
                  {blocked.map((b) => (
                    <li key={b.id}>
                      <strong>{b.name}</strong> · {b.campaigns.join(", ")}
                    </li>
                  ))}
                </ul>
                <div className={styles.blockedActions}>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={blocked.length >= chosen.length}
                    onClick={() =>
                      setLeft((prev) => new Set([...prev, ...blocked.map((b) => b.id)]))
                    }
                  >
                    Leave {blocked.length === 1 ? "it" : "them"} out
                  </Button>
                  <span className={styles.hint}>
                    Or remove them from the campaign first.
                  </span>
                </div>
              </div>
            )}

            <div className={styles.options}>
              <label className={styles.check}>
                <input
                  type="checkbox"
                  checked={suppress}
                  onChange={(e) => setSuppress(e.target.checked)}
                />
                <span>Don&rsquo;t suggest these again here</span>
              </label>
              <p className={styles.checkHint}>
                Populate and daily discovery won&rsquo;t offer these companies to this workspace.
              </p>

              <label className={styles.check}>
                <input
                  type="checkbox"
                  checked={report}
                  onChange={(e) => setReport(e.target.checked)}
                />
                <span>Report wrong company data</span>
              </label>
              <p className={styles.checkHint}>
                Flags the shared records for our team to check. Nothing other workspaces see
                changes until it is checked.
              </p>
              {report && (
                <Field
                  label="What is wrong?"
                  required
                  hint="Needed to send the report, so it can be checked."
                >
                  <Textarea
                    rows={3}
                    value={reason}
                    maxLength={500}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder="For example: the website belongs to a different company"
                  />
                </Field>
              )}
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
