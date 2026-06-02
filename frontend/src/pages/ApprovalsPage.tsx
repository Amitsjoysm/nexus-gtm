import { useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Icons,
  Input,
  Skeleton,
  Tabs,
  Textarea,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { APPROVAL_STATUS_TONE } from "@/lib/runStatus";
import type { Approval, ApprovalStatus } from "@/lib/types";
import styles from "./ApprovalsPage.module.css";

const TABS: { value: ApprovalStatus; label: string }[] = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
];

export function ApprovalsPage() {
  const api = useApiClient();
  const [status, setStatus] = useState<ApprovalStatus>("pending");

  const approvals = useApi<Approval[]>(
    (signal) => api.listApprovals(status, signal),
    [status],
  );

  return (
    <div>
      <PageHeader
        title="Approvals"
        description="The human gate before any AI-drafted message leaves the building. Review, edit, and approve or reject each send."
      />

      <Tabs
        aria-label="Approval status"
        value={status}
        onChange={(v) => setStatus(v as ApprovalStatus)}
        items={TABS.map((t) => ({
          value: t.value,
          label: t.label,
          count: t.value === status ? approvals.data?.length : undefined,
        }))}
      />

      <div className={styles.panel}>
        <DataState
          state={approvals}
          errorTitle="Couldn't load approvals"
          skeleton={
            <div className={styles.list}>
              {[0, 1].map((i) => (
                <Card key={i} padding="lg">
                  <Skeleton width="30%" height={16} />
                  <div style={{ height: "var(--space-3)" }} />
                  <Skeleton width="100%" height={80} />
                </Card>
              ))}
            </div>
          }
          isEmpty={(rows) => rows.length === 0}
          empty={
            <EmptyState
              icon={<Icons.ShieldCheckIcon />}
              title={status === "pending" ? "Nothing waiting" : `No ${status} approvals`}
              description={
                status === "pending"
                  ? "When an AI run drafts outreach, it parks here for your sign-off."
                  : "Decisions you make appear under their status."
              }
            />
          }
        >
          {(rows) => (
            <div className={styles.list}>
              {rows.map((approval) => (
                <ApprovalCard
                  key={approval.id}
                  approval={approval}
                  onDecided={() => approvals.refetch()}
                />
              ))}
            </div>
          )}
        </DataState>
      </div>
    </div>
  );
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function ApprovalCard({
  approval,
  onDecided,
}: {
  approval: Approval;
  onDecided: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const pending = approval.status === "pending";

  const draftSubject = str(approval.payload.subject);
  const draftBody = str(approval.payload.body) || str(approval.payload.message);
  const grounded = approval.payload.grounded === true;

  const [subject, setSubject] = useState(draftSubject);
  const [body, setBody] = useState(draftBody);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    try {
      const edits =
        decision === "approve" && editing
          ? { subject: subject.trim(), body: body.trim() }
          : undefined;
      await api.decideApproval(approval.id, { decision, edits });
      toast.success(
        decision === "approve" ? "Approved" : "Rejected",
        decision === "approve" ? "The message was sent to the sequence." : "The send was skipped.",
      );
      onDecided();
    } catch (err) {
      toast.error(
        "Couldn't record your decision",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card padding="lg" className={styles.card}>
      <div className={styles.cardHead}>
        <div className={styles.cardHeadLeft}>
          <Badge tone={APPROVAL_STATUS_TONE[approval.status]} dot>
            {approval.status[0].toUpperCase() + approval.status.slice(1)}
          </Badge>
          <span className={styles.kind}>{str(approval.kind) || "Outreach"}</span>
        </div>
        <Link to={`/runs/${approval.run_id}`} className={styles.runLink}>
          View run <Icons.ChevronRightIcon />
        </Link>
      </div>

      <div className={styles.draft}>
        {editing ? (
          <>
            <Field label="Subject">
              <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
            </Field>
            <Field label="Message">
              <Textarea rows={8} value={body} onChange={(e) => setBody(e.target.value)} />
            </Field>
          </>
        ) : (
          <>
            <div className={styles.draftTop}>
              {(subject || draftSubject) && (
                <div className={styles.subject}>
                  <span className={styles.subjectLabel}>Subject</span>
                  {subject || draftSubject}
                </div>
              )}
              {grounded && <Badge tone="success">Grounded</Badge>}
            </div>
            <pre className={styles.body}>{body || draftBody || "No message content."}</pre>
          </>
        )}
      </div>

      {pending ? (
        <div className={styles.actions}>
          <Button
            variant="ghost"
            iconLeft={<Icons.FileTextIcon />}
            onClick={() => setEditing((v) => !v)}
            disabled={busy !== null}
          >
            {editing ? "Preview" : "Edit draft"}
          </Button>
          <div className={styles.actionsRight}>
            <Button
              variant="secondary"
              iconLeft={<Icons.XIcon />}
              loading={busy === "reject"}
              disabled={busy === "approve"}
              onClick={() => decide("reject")}
            >
              Reject
            </Button>
            <Button
              iconLeft={<Icons.SendIcon />}
              loading={busy === "approve"}
              disabled={busy === "reject"}
              onClick={() => decide("approve")}
            >
              Approve &amp; send
            </Button>
          </div>
        </div>
      ) : (
        <div className={styles.decided}>
          {approval.decided_at ? `Decided ${timeAgo(approval.decided_at)}` : "Decided"}
        </div>
      )}
    </Card>
  );
}
