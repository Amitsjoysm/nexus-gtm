import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { campaignTone, targetTone } from "@/lib/display";
import { formatNumber, humanize, timeAgo } from "@/lib/format";
import type {
  Campaign,
  CampaignDetail,
  CampaignInput,
  CampaignPreview,
  CampaignProgress,
  CampaignTarget,
} from "@/lib/types";
import styles from "./CampaignsPage.module.css";

const DEFAULT_SEQUENCE = "ai-orchestrated-outbound";

/** Statuses where the send pipeline is still moving, so we open the live progress stream. */
const STREAMING = new Set(["drafting", "approved", "sending"]);

function statusLabel(status: string): string {
  return humanize(status);
}

/** Pull a human-readable subject/body out of a target's opaque draft blob. */
function draftText(draft: Record<string, unknown>): { subject: string; body: string } {
  const subject = typeof draft.subject === "string" ? draft.subject : "";
  const body =
    typeof draft.body === "string"
      ? draft.body
      : typeof draft.message === "string"
        ? draft.message
        : "";
  return { subject, body };
}

export function CampaignsPage() {
  const api = useApiClient();
  const toast = useToast();

  const campaigns = useApi<Campaign[]>((signal) => api.listCampaigns(signal), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  // Default the selection to the newest campaign once the list arrives.
  useEffect(() => {
    if (selectedId == null && campaigns.data && campaigns.data.length > 0) {
      setSelectedId(campaigns.data[0].id);
    }
  }, [campaigns.data, selectedId]);

  const onCreated = useCallback(
    (created: Campaign) => {
      campaigns.refetch();
      setSelectedId(created.id);
      setCreateOpen(false);
    },
    [campaigns],
  );

  return (
    <div>
      <PageHeader
        title="Campaigns"
        description="Draft AI outreach over a saved list, review the sample, approve once, and send."
        actions={
          <Button iconLeft={<Icons.PlusIcon />} onClick={() => setCreateOpen(true)}>
            New campaign
          </Button>
        }
      />

      <div className={styles.layout}>
        <DataState
          state={campaigns}
          errorTitle="Couldn't load campaigns"
          skeleton={
            <div className={styles.list}>
              {[0, 1, 2].map((i) => (
                <Card key={i} padding="md">
                  <Skeleton width="60%" height={16} />
                  <div style={{ height: "var(--space-3)" }} />
                  <Skeleton width="40%" height={12} />
                </Card>
              ))}
            </div>
          }
          isEmpty={(rows) => rows.length === 0}
          empty={
            <EmptyState
              icon={<Icons.SendIcon />}
              title="No campaigns yet"
              description="Create a campaign to draft and send AI outreach across a saved list of accounts."
              action={
                <Button iconLeft={<Icons.PlusIcon />} onClick={() => setCreateOpen(true)}>
                  New campaign
                </Button>
              }
            />
          }
        >
          {(rows) => (
            <ul className={styles.list} aria-label="Campaigns">
              {rows.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    className={styles.row}
                    aria-current={c.id === selectedId}
                    onClick={() => setSelectedId(c.id)}
                  >
                    <span className={styles.rowMain}>
                      <span className={styles.rowName}>{c.name}</span>
                      <span className={styles.rowMeta}>Created {timeAgo(c.created_at)}</span>
                    </span>
                    <Badge tone={campaignTone(c.status)} dot>
                      {statusLabel(c.status)}
                    </Badge>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </DataState>

        <div className={styles.detail}>
          {selectedId ? (
            <CampaignDetailPanel
              key={selectedId}
              campaignId={selectedId}
              onChanged={() => campaigns.refetch()}
            />
          ) : (
            <Card padding="lg" className={styles.detailEmpty}>
              <EmptyState
                compact
                icon={<Icons.SendIcon />}
                title="Select a campaign"
                description="Pick a campaign on the left to review its targets and progress."
              />
            </Card>
          )}
        </div>
      </div>

      {createOpen && (
        <CreateCampaignModal
          onClose={() => setCreateOpen(false)}
          onCreated={onCreated}
          onError={(msg) => toast.error("Couldn't create campaign", msg)}
        />
      )}
    </div>
  );
}

function CampaignDetailPanel({
  campaignId,
  onChanged,
}: {
  campaignId: string;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const detail = useApi<CampaignDetail>((signal) => api.getCampaign(campaignId, signal), [campaignId]);
  const [progress, setProgress] = useState<CampaignProgress | null>(null);
  const [acting, setActing] = useState(false);

  const status = progress?.status ?? detail.data?.status ?? "";

  // While the send pipeline is moving, follow the SSE stream for live counts, then reconcile.
  const streaming = STREAMING.has(status);
  useEffect(() => {
    if (!streaming) return;
    const controller = new AbortController();
    let closed = false;
    api
      .streamCampaignEvents(campaignId, { onProgress: (p) => setProgress(p) }, controller.signal)
      .catch(() => {})
      .finally(() => {
        if (!closed) {
          detail.refetch();
          onChanged();
        }
      });
    return () => {
      closed = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming, campaignId]);

  const approve = useCallback(async () => {
    setActing(true);
    try {
      await api.approveCampaign(campaignId);
      toast.success("Campaign approved", "Sending the approved drafts now.");
      setProgress(null);
      detail.refetch();
      onChanged();
    } catch (err) {
      toast.error("Couldn't approve", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setActing(false);
    }
  }, [api, campaignId, detail, onChanged, toast]);

  const cancel = useCallback(async () => {
    setActing(true);
    try {
      await api.cancelCampaign(campaignId);
      toast.success("Campaign cancelled");
      setProgress(null);
      detail.refetch();
      onChanged();
    } catch (err) {
      toast.error("Couldn't cancel", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setActing(false);
    }
  }, [api, campaignId, detail, onChanged, toast]);

  return (
    <DataState
      state={detail}
      errorTitle="Couldn't load this campaign"
      skeleton={
        <Card padding="lg">
          <Skeleton width="50%" height={20} />
          <div style={{ height: "var(--space-4)" }} />
          <Skeleton width="100%" height={64} />
        </Card>
      }
    >
      {(c) => {
        const counts = progress?.counts ?? deriveCounts(c.targets);
        const awaiting = status === "awaiting_approval";
        const terminal = ["completed", "cancelled", "failed"].includes(status);
        return (
          <div className={styles.detailStack}>
            <Card padding="lg">
              <div className={styles.detailHead}>
                <div className={styles.detailTitleWrap}>
                  <h3 className={styles.detailTitle}>{c.name}</h3>
                  <p className={styles.detailSub}>
                    {formatNumber(c.targets.length)} targets · sequence{" "}
                    <code className={styles.code}>{c.sequence}</code>
                  </p>
                </div>
                <Badge tone={campaignTone(status)} dot>
                  {statusLabel(status)}
                </Badge>
              </div>

              <CountBar counts={counts} />

              {Object.values(c.outcomes ?? {}).some((n) => n > 0) && (
                <div className={styles.results} aria-label="Attributed results">
                  <span className={styles.resultsLabel}>Results</span>
                  {(["replied", "meeting", "won", "lost"] as const)
                    .filter((s) => (c.outcomes[s] ?? 0) > 0)
                    .map((s) => (
                      <Badge
                        key={s}
                        tone={s === "won" ? "success" : s === "lost" ? "neutral" : "info"}
                        dot
                      >
                        {humanize(s)} {formatNumber(c.outcomes[s])}
                      </Badge>
                    ))}
                </div>
              )}

              {(awaiting || streaming) && (
                <div className={styles.actions}>
                  {awaiting && (
                    <Button onClick={approve} loading={acting} iconLeft={<Icons.CheckIcon />}>
                      Approve &amp; send
                    </Button>
                  )}
                  <Button variant="secondary" onClick={cancel} disabled={acting}>
                    Cancel campaign
                  </Button>
                </div>
              )}

              {terminal && (
                <p className={styles.terminalNote}>
                  {status === "completed"
                    ? "All targets resolved. Sent drafts went to the configured sequence."
                    : status === "cancelled"
                      ? "This campaign was cancelled before sending."
                      : "This campaign failed. Check the targets below for errors."}
                </p>
              )}
            </Card>

            {awaiting && <PreviewSample campaignId={campaignId} />}

            <TargetsCard targets={c.targets} />
          </div>
        );
      }}
    </DataState>
  );
}

function deriveCounts(targets: CampaignTarget[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const t of targets) counts[t.status] = (counts[t.status] ?? 0) + 1;
  return counts;
}

function CountBar({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  if (entries.length === 0) {
    return <p className={styles.muted}>No targets enumerated yet.</p>;
  }
  return (
    <div className={styles.counts}>
      {entries.map(([status, n]) => (
        <span key={status} className={styles.countChip}>
          <Badge tone={targetTone(status)} dot>
            {humanize(status)}
          </Badge>
          <span className={styles.countNum}>{formatNumber(n)}</span>
        </span>
      ))}
    </div>
  );
}

function PreviewSample({ campaignId }: { campaignId: string }) {
  const api = useApiClient();
  const preview = useApi<CampaignPreview>((signal) => api.previewCampaign(campaignId, signal), [campaignId]);

  return (
    <Card padding="lg">
      <CardHeader
        title="Approval sample"
        subtitle="A few drafted messages, so you can spot-check before anything sends."
      />
      <DataState
        state={preview}
        errorTitle="Couldn't load the sample"
        skeleton={<Skeleton width="100%" height={120} />}
        isEmpty={(p) => p.sample.length === 0}
        empty={
          <EmptyState
            compact
            icon={<Icons.FileTextIcon />}
            title="No deliverable drafts"
            description="Every target was skipped (no reachable contact or ungrounded draft). Check the targets list."
          />
        }
      >
        {(p) => (
          <ol className={styles.samples}>
            {p.sample.map((t) => {
              const { subject, body } = draftText(t.draft);
              return (
                <li key={t.id} className={styles.sample}>
                  <div className={styles.sampleSubject}>{subject || "(no subject)"}</div>
                  {body && <p className={styles.sampleBody}>{body}</p>}
                </li>
              );
            })}
          </ol>
        )}
      </DataState>
    </Card>
  );
}

function TargetsCard({ targets }: { targets: CampaignTarget[] }) {
  if (targets.length === 0) return null;
  return (
    <Card padding="lg">
      <CardHeader title="Targets" subtitle={`${formatNumber(targets.length)} accounts in this campaign`} />
      <ul className={styles.targets}>
        {targets.map((t) => {
          const { subject } = draftText(t.draft);
          return (
            <li key={t.id} className={styles.target}>
              <Badge tone={targetTone(t.status)} dot>
                {humanize(t.status)}
              </Badge>
              <span className={styles.targetText}>
                {subject || <code className={styles.code}>{t.account_id.slice(0, 8)}</code>}
              </span>
              {t.skip_reason && <span className={styles.targetNote}>{humanize(t.skip_reason)}</span>}
              {t.error && <span className={styles.targetError}>{t.error}</span>}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function CreateCampaignModal({
  onClose,
  onCreated,
  onError,
}: {
  onClose: () => void;
  onCreated: (c: Campaign) => void;
  onError: (msg: string) => void;
}) {
  const api = useApiClient();
  const lists = useApi((signal) => api.listSavedLists(signal), []);
  const cadences = useApi((signal) => api.listCadences(signal), []);

  const [name, setName] = useState("");
  const [listId, setListId] = useState("");
  const [cadenceId, setCadenceId] = useState("");
  const [sequence, setSequence] = useState(DEFAULT_SEQUENCE);
  const [sendRisky, setSendRisky] = useState(false);
  const [reviewEach, setReviewEach] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // Default the list selection to the newest saved list once they load.
  useEffect(() => {
    if (!listId && lists.data && lists.data.length > 0) setListId(lists.data[0].id);
  }, [lists.data, listId]);

  const listOptions = useMemo(
    () => (lists.data ?? []).map((l) => ({ value: l.id, label: `${l.name} (${l.accounts})` })),
    [lists.data],
  );
  const cadenceOptions = useMemo(
    () => [
      { value: "", label: "No cadence (single touch)" },
      ...(cadences.data ?? []).filter((c) => c.is_active).map((c) => ({ value: c.id, label: c.name })),
    ],
    [cadences.data],
  );

  const noLists = lists.data != null && lists.data.length === 0;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const body: CampaignInput = {
        name: name.trim(),
        list_id: listId,
        sequence: sequence.trim() || DEFAULT_SEQUENCE,
        send_risky: sendRisky,
        cadence_id: cadenceId || null,
        review_each_touch: cadenceId ? reviewEach : false,
      };
      const created = await api.createCampaign(body);
      onCreated(created);
    } catch (err) {
      onError(err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="New campaign"
      description="Drafting starts immediately and parks at the approval gate. Nothing sends until you approve."
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            form="new-campaign-form"
            type="submit"
            loading={submitting}
            disabled={!name.trim() || !listId}
          >
            Create &amp; draft
          </Button>
        </>
      }
    >
      <form id="new-campaign-form" className={styles.form} onSubmit={onSubmit} noValidate>
        <Field label="Campaign name" required>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Q3 expansion — Series B fintechs"
            required
          />
        </Field>

        <Field
          label="Target list"
          required
          hint={noLists ? undefined : "Accounts in this saved segment become the campaign's targets."}
          error={noLists ? "You have no saved lists yet." : undefined}
        >
          {noLists ? (
            <p className={styles.formNote}>
              <Link to="/lists" className={styles.link}>
                Build a list
              </Link>{" "}
              first, then come back to launch a campaign over it.
            </p>
          ) : (
            <Select
              value={listId}
              onChange={(e) => setListId(e.target.value)}
              options={listOptions}
              disabled={lists.loading}
            />
          )}
        </Field>

        <Field label="Cadence" hint="Optional. Adds follow-up touches over time instead of a single send.">
          <Select
            value={cadenceId}
            onChange={(e) => setCadenceId(e.target.value)}
            options={cadenceOptions}
            disabled={cadences.loading}
          />
        </Field>

        <Field label="Sequence" hint="The sales-engagement sequence approved drafts are pushed into.">
          <Input value={sequence} onChange={(e) => setSequence(e.target.value)} placeholder={DEFAULT_SEQUENCE} />
        </Field>

        <label className={styles.toggle}>
          <input type="checkbox" checked={sendRisky} onChange={(e) => setSendRisky(e.target.checked)} />
          <span>
            Send to risky addresses
            <span className={styles.toggleHint}>
              Off by default. When off, contacts with a risky deliverability verdict are skipped.
            </span>
          </span>
        </label>

        {cadenceId && (
          <label className={styles.toggle}>
            <input type="checkbox" checked={reviewEach} onChange={(e) => setReviewEach(e.target.checked)} />
            <span>
              Review each touch
              <span className={styles.toggleHint}>Pause every follow-up for approval before it sends.</span>
            </span>
          </label>
        )}
      </form>
    </Modal>
  );
}
