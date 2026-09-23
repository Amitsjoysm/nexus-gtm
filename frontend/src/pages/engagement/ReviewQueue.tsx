import { useState } from "react";
import {
  Badge, Button, EmptyState, ErrorState, Field, Icons, Input, Skeleton, Textarea, WorkingIndicator,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ReviewItem } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Read every opening email before it goes (spec §9 step 4, D22).
 *
 * Each person's draft is shown with the quality check's complaints beside it. The SDR edits in
 * place and approves, regenerates, or takes the person out. "Approve all that pass" approves only
 * drafts the check found nothing wrong with: a flagged draft always needs a person to look.
 *
 * Approving on a running campaign sends that person's email at once (or at the scheduled start),
 * so the queue is useful after launch too: people added later are reviewed here the same way.
 */

const STATUS: Record<string, { label: string; tone: "neutral" | "info" | "success" }> = {
  undrafted: { label: "Not written yet", tone: "neutral" },
  draft: { label: "Draft", tone: "info" },
  approved: { label: "Approved", tone: "success" },
};

export interface ReviewQueueProps {
  campaignId: string;
  /** Something changed that the rest of the page counts (approvals, removals, new drafts). */
  onChanged: () => void;
}

export function ReviewQueue({ campaignId, onChanged }: ReviewQueueProps) {
  const api = useApiClient();
  const toast = useToast();
  const review = useApi<ReviewItem[]>((s) => api.campaignReview(campaignId, s), [campaignId]);
  const [drafting, setDrafting] = useState<{ done: number; of: number } | null>(null);
  const [approvingAll, setApprovingAll] = useState(false);

  const items = review.data ?? [];
  const undrafted = items.filter((i) => i.status === "undrafted").length;
  const passing = items.filter((i) => i.status === "draft" && i.quality_problems.length === 0).length;
  const flagged = items.filter((i) => i.status === "draft" && i.quality_problems.length > 0).length;
  const approved = items.filter((i) => i.status === "approved").length;

  async function draftAll() {
    const of = undrafted;
    let done = 0;
    setDrafting({ done, of });
    try {
      // The server drafts in passes so a long list is not one request. A pass that writes nothing
      // is the end: either everyone has a draft, or what is left keeps failing and needs a look.
      for (;;) {
        const pass = await api.draftCampaign(campaignId, 10);
        done += pass.drafted;
        setDrafting({ done, of });
        review.refetch();
        if (pass.errors.some((e) => e.error === "out_of_credits")) {
          toast.error("Out of credits", "Drafting stopped. Top up the workspace's credits to write the rest.");
          break;
        }
        if (pass.drafted === 0) {
          if (pass.failed > 0) {
            toast.error(`${pass.failed} could not be written`, "Try Regenerate on each, or take them out.");
          }
          break;
        }
      }
      if (done > 0) toast.success(`${done} opening ${done === 1 ? "email" : "emails"} written`, "Read each one, then approve.");
    } catch (err) {
      toast.error("Drafting stopped", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setDrafting(null);
      review.refetch();
      onChanged();
    }
  }

  async function approveAll() {
    setApprovingAll(true);
    try {
      const { approved: n } = await api.approveAllPassing(campaignId);
      toast.success(`${n} approved`, flagged ? `${flagged} flagged ${flagged === 1 ? "draft still needs" : "drafts still need"} a look.` : "");
      review.refetch();
      onChanged();
    } catch (err) {
      toast.error("Couldn't approve", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setApprovingAll(false);
    }
  }

  if (review.error) {
    return <ErrorState title="Couldn't load the review queue" message={review.error.detail} onRetry={review.refetch} />;
  }
  if (review.loading && !review.data) {
    return (
      <div className={styles.stack}>
        <Skeleton width="100%" height={56} />
        <Skeleton width="100%" height={220} />
        <Skeleton width="100%" height={220} />
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <EmptyState
        icon={<Icons.CheckIcon />}
        title="Nobody is waiting for review"
        description="Everyone added so far has been approved or has already started. People you add next appear here."
      />
    );
  }

  return (
    <div className={styles.stack}>
      <div className={styles.toolbar}>
        <p className={styles.summary} aria-live="polite">
          <span><strong>{approved}</strong> approved</span>
          <span><strong>{passing}</strong> ready</span>
          {flagged > 0 && <span><strong>{flagged}</strong> flagged</span>}
          {undrafted > 0 && <span><strong>{undrafted}</strong> not written</span>}
        </p>
        <div className={styles.toolbarActions}>
          {undrafted > 0 && (
            <Button variant="secondary" iconLeft={<Icons.SparklesIcon />} onClick={draftAll} disabled={drafting !== null}>
              Write {undrafted} opening {undrafted === 1 ? "email" : "emails"}
            </Button>
          )}
          <Button onClick={approveAll} loading={approvingAll} disabled={passing === 0 || drafting !== null}
            iconLeft={<Icons.CheckIcon />}>
            Approve all that pass
          </Button>
        </div>
      </div>

      {drafting && (
        <WorkingIndicator
          label={`Writing opening emails: ${drafting.done} of ${drafting.of}`}
          hint="Each one is written from what the person and their company are doing now."
          slowAfter={30}
        />
      )}

      <ol className={styles.reviewList}>
        {items.map((item) => (
          <ReviewCard key={item.enrollment_id} item={item}
            onChanged={() => { review.refetch(); onChanged(); }} />
        ))}
      </ol>
    </div>
  );
}

function ReviewCard({ item, onChanged }: { item: ReviewItem; onChanged: () => void }) {
  const api = useApiClient();
  const toast = useToast();
  const [subject, setSubject] = useState(item.subject);
  const [body, setBody] = useState(item.body);
  const [busy, setBusy] = useState<"approve" | "regenerate" | "remove" | null>(null);
  const status = STATUS[item.status] ?? { label: item.status, tone: "neutral" as const };
  const editable = item.status === "draft" && item.message_id !== null;
  const edited = subject !== item.subject || body !== item.body;

  async function run(kind: NonNullable<typeof busy>, action: () => Promise<unknown>, done?: string) {
    setBusy(kind);
    try {
      await action();
      if (done) toast.success(done, "");
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className={styles.reviewCard}>
      <header className={styles.reviewHead}>
        <div className={styles.person}>
          <span className={styles.personName}>{item.contact_name || item.contact_email}</span>
          <span className={styles.muted}>
            {[item.contact_title, item.account_name].filter(Boolean).join(" · ")}
          </span>
          <span className={styles.muted}>{item.contact_email}</span>
        </div>
        <Badge tone={status.tone} dot>{status.label}</Badge>
      </header>

      {/* The check read the AI's text; once a person has approved it, those notes are history. */}
      {item.status === "draft" && item.quality_problems.length > 0 && (
        // Sentences, not tags: each one says what to change, so it has to wrap and be read.
        <ul className={styles.problems} aria-label="What the check found">
          {item.quality_problems.map((p) => (
            <li key={p} className={styles.problem}>
              <Icons.AlertTriangleIcon aria-hidden className={styles.problemIcon} />
              <span>{p}</span>
            </li>
          ))}
        </ul>
      )}

      {busy === "regenerate" ? (
        <WorkingIndicator label={`Rewriting the email to ${item.contact_name || "this person"}`} hint="Usually under 15 seconds." slowAfter={20} />
      ) : item.status === "undrafted" ? (
        <p className={styles.muted}>No draft yet. Use "Write opening emails" above.</p>
      ) : editable ? (
        <div className={styles.draft}>
          <Field label="Subject">
            <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
          </Field>
          <Field label="Email">
            <Textarea rows={9} value={body} onChange={(e) => setBody(e.target.value)} />
          </Field>
        </div>
      ) : (
        <div className={styles.draftRead}>
          <p className={styles.subjectLine}>{item.subject}</p>
          <p className={styles.bodyText}>{item.body}</p>
        </div>
      )}

      <div className={styles.cardActions}>
        {editable && (
          <Button
            loading={busy === "approve"} disabled={busy !== null || !body.trim()}
            iconLeft={<Icons.CheckIcon />}
            onClick={() => run("approve", () => api.approveCampaignMessage(
              item.message_id as string, edited ? { subject, body } : {}), edited ? "Approved with your edits" : "Approved")}
          >
            {edited ? "Approve with edits" : "Approve"}
          </Button>
        )}
        {item.message_id && item.status !== "undrafted" && (
          <Button
            variant="secondary" disabled={busy !== null} iconLeft={<Icons.RefreshIcon />}
            onClick={() => run("regenerate", async () => {
              const fresh = await api.regenerateCampaignMessage(item.message_id as string);
              setSubject(fresh.subject);
              setBody(fresh.body);
            })}
          >
            Regenerate
          </Button>
        )}
        <Button
          variant="ghost" loading={busy === "remove"} disabled={busy !== null}
          onClick={() => run("remove", () => api.enrollmentAction(item.enrollment_id, "remove"), "Taken out of the campaign")}
        >
          Take out
        </Button>
      </div>
    </li>
  );
}
