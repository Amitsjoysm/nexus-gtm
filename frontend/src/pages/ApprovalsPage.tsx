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
  Select,
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
import { APPROVAL_STATUS_TONE, EMAIL_STATUS_META, asEmailStatus } from "@/lib/runStatus";
import type { Approval, ApprovalPayload, ApprovalStatus, Mailbox } from "@/lib/types";
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
  // Send-ready mailboxes for the "Send from" picker. Read-only for approvers; empty when the
  // workspace hasn't configured SMTP (then sends record-only and no picker shows).
  const mailboxes = useApi<Mailbox[]>((signal) => api.listMailboxes(signal), []);

  return (
    <div>
      <PageHeader
        title="Approvals"
        description="The human gate before any AI-drafted message leaves the building. Review, redraft, and approve or reject each send."
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
                  mailboxes={mailboxes.data ?? []}
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
  mailboxes,
  onDecided,
}: {
  approval: Approval;
  mailboxes: Mailbox[];
  onDecided: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const pending = approval.status === "pending";

  const payload = approval.payload as ApprovalPayload;
  const draftSubject = str(payload.subject);
  const draftBody = str(payload.body) || str(payload.message);
  const grounded = payload.grounded === true;
  const facts = (payload.grounding?.facts ?? []).filter((f) => f.trim());
  const emailStatus = asEmailStatus(payload.email_status);
  const emailMeta = emailStatus ? EMAIL_STATUS_META[emailStatus] : null;
  // The backend gate refuses an ungrounded or undeliverable send regardless of
  // the reviewer's decision — surface that here so approving isn't a surprise.
  const willBeBlocked = pending && (!grounded || emailStatus === "invalid");

  const [subject, setSubject] = useState(draftSubject);
  const [body, setBody] = useState(draftBody);
  const [editing, setEditing] = useState(false);
  const [redrafting, setRedrafting] = useState(false);
  const [instructions, setInstructions] = useState("");
  const [redraftBusy, setRedraftBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);

  const defaultMailbox = mailboxes.find((m) => m.default)?.id ?? mailboxes[0]?.id ?? "";
  const [fromAccount, setFromAccount] = useState("");
  const selectedMailbox = fromAccount || defaultMailbox;

  async function redraft() {
    const text = instructions.trim();
    if (!text) return;
    setRedraftBusy(true);
    try {
      const updated = await api.redraftApproval(approval.id, text);
      const p = updated.payload as ApprovalPayload;
      setSubject(str(p.subject));
      setBody(str(p.body) || str(p.message));
      setInstructions("");
      setRedrafting(false);
      setEditing(false);
      toast.success("Draft regenerated", "Review the new version before sending.");
    } catch (err) {
      toast.error(
        "Couldn't regenerate",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setRedraftBusy(false);
    }
  }

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    try {
      if (decision === "approve") {
        await api.decideApproval(approval.id, {
          decision,
          edits: editing ? { subject: subject.trim(), body: body.trim() } : undefined,
          from_account: selectedMailbox || undefined,
        });
        toast.success("Approved", "The message was sent to the sequence.");
      } else {
        await api.decideApproval(approval.id, {
          decision,
          reason: reason.trim() || undefined,
        });
        toast.success("Rejected", "The send was skipped.");
      }
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
          {payload.redrafted === true && <Badge tone="info">Redrafted</Badge>}
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
              <div className={styles.signals}>
                <Badge tone={grounded ? "success" : "danger"}>
                  {grounded ? "Grounded" : "Ungrounded"}
                </Badge>
                {emailMeta && <Badge tone={emailMeta.tone}>{emailMeta.label}</Badge>}
              </div>
            </div>
            <pre className={styles.body}>{body || draftBody || "No message content."}</pre>
            {facts.length > 0 && (
              <div className={styles.facts}>
                <span className={styles.factsLabel}>Grounded in</span>
                <ul className={styles.factList}>
                  {facts.map((fact, i) => (
                    <li key={i}>{fact}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>

      {pending && redrafting && (
        <div className={styles.redraftBox}>
          <Field
            label="Tell the AI how to revise this"
            hint="It rewrites the message grounded in the same research facts."
          >
            <Textarea
              rows={3}
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder="e.g. Make it two sentences, lead with the funding signal, mention SOC 2."
            />
          </Field>
          <div className={styles.boxActions}>
            <Button variant="ghost" onClick={() => setRedrafting(false)} disabled={redraftBusy}>
              Cancel
            </Button>
            <Button
              variant="secondary"
              iconLeft={<Icons.SparklesIcon />}
              loading={redraftBusy}
              disabled={!instructions.trim()}
              onClick={redraft}
            >
              Regenerate draft
            </Button>
          </div>
        </div>
      )}

      {willBeBlocked && (
        <div className={styles.warning} role="alert">
          <Icons.AlertTriangleIcon />
          <span>
            {!grounded
              ? "This draft isn't grounded in research facts, so the send will be refused. Redraft with research, or reject it."
              : "The recipient address is undeliverable, so the send will be refused. Fix the contact or reject it."}
          </span>
        </div>
      )}

      {pending && rejecting && (
        <div className={styles.rejectBox}>
          <Field label="Reason" hint="Optional. The rep who owns the account will see this.">
            <Textarea
              rows={2}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. Wrong persona — target the VP of Eng, not the recruiter."
            />
          </Field>
          <div className={styles.boxActions}>
            <Button variant="ghost" onClick={() => setRejecting(false)} disabled={busy !== null}>
              Cancel
            </Button>
            <Button
              variant="secondary"
              iconLeft={<Icons.XIcon />}
              loading={busy === "reject"}
              onClick={() => decide("reject")}
            >
              Confirm rejection
            </Button>
          </div>
        </div>
      )}

      {pending && !rejecting ? (
        <div className={styles.actions}>
          <div className={styles.actionsLeft}>
            <Button
              variant="ghost"
              iconLeft={<Icons.FileTextIcon />}
              onClick={() => setEditing((v) => !v)}
              disabled={busy !== null}
            >
              {editing ? "Preview" : "Edit draft"}
            </Button>
            <Button
              variant="ghost"
              iconLeft={<Icons.SparklesIcon />}
              onClick={() => setRedrafting((v) => !v)}
              disabled={busy !== null}
            >
              Redraft with AI
            </Button>
          </div>
          <div className={styles.actionsRight}>
            {mailboxes.length > 0 && (
              <Select
                aria-label="Send from mailbox"
                value={selectedMailbox}
                onChange={(e) => setFromAccount(e.target.value)}
                options={mailboxes.map((m) => ({
                  value: m.id,
                  label: `From: ${m.label || m.from_email}`,
                }))}
              />
            )}
            <Button
              variant="secondary"
              iconLeft={<Icons.XIcon />}
              disabled={busy !== null}
              onClick={() => setRejecting(true)}
            >
              Reject
            </Button>
            <Button
              iconLeft={<Icons.SendIcon />}
              loading={busy === "approve"}
              disabled={busy !== null || willBeBlocked}
              title={willBeBlocked ? "Blocked by the grounded + verified send gate" : undefined}
              onClick={() => decide("approve")}
            >
              Approve &amp; send
            </Button>
          </div>
        </div>
      ) : !pending ? (
        <div className={styles.decided}>
          {approval.decided_at ? `Decided ${timeAgo(approval.decided_at)}` : "Decided"}
          {str(approval.edits?.reason) && (
            <span className={styles.reasonNote}> · {str(approval.edits?.reason)}</span>
          )}
        </div>
      ) : null}
    </Card>
  );
}

export default ApprovalsPage;
