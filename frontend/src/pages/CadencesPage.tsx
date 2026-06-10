import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Field,
  Icons,
  IconButton,
  Input,
  Modal,
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
import { enrollmentTone, touchTone } from "@/lib/display";
import { formatNumber, humanize, timeAgo } from "@/lib/format";
import type {
  Cadence,
  CadenceEnrollment,
  CadenceInput,
  CadenceReport,
  CadenceStepInput,
  Campaign,
  EnrollmentDetail,
} from "@/lib/types";
import styles from "./CadencesPage.module.css";

const CHANNEL_OPTIONS = [
  { value: "email", label: "Email" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "call", label: "Call" },
  { value: "sms", label: "SMS" },
];

const blankStep = (): CadenceStepInput => ({ delay_days: 2, channel: "email", angle: "" });

type TabValue = "cadences" | "enrollments";

export function CadencesPage() {
  const [tab, setTab] = useState<TabValue>("cadences");
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <div>
      <PageHeader
        title="Cadences"
        description="Multi-touch sequences: define the steps once, then run them across a campaign's accounts."
        actions={
          tab === "cadences" ? (
            <Button iconLeft={<Icons.PlusIcon />} onClick={() => setCreateOpen(true)}>
              New cadence
            </Button>
          ) : undefined
        }
      />

      <Tabs
        className={styles.tabs}
        aria-label="Cadence views"
        items={[
          { value: "cadences", label: "Cadences" },
          { value: "enrollments", label: "Enrollments" },
        ]}
        value={tab}
        onChange={(v) => setTab(v as TabValue)}
      />

      {tab === "cadences" ? (
        <CadenceLibrary createOpen={createOpen} setCreateOpen={setCreateOpen} />
      ) : (
        <EnrollmentsTab />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ Cadence library */

function CadenceLibrary({
  createOpen,
  setCreateOpen,
}: {
  createOpen: boolean;
  setCreateOpen: (open: boolean) => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const cadences = useApi<Cadence[]>((signal) => api.listCadences(signal), []);

  const deactivate = useCallback(
    async (c: Cadence) => {
      try {
        await api.deactivateCadence(c.id);
        toast.success("Cadence deactivated", `${c.name} won't enroll new accounts.`);
        cadences.refetch();
      } catch (err) {
        toast.error("Couldn't deactivate", err instanceof ApiError ? err.detail : "Please try again.");
      }
    },
    [api, cadences, toast],
  );

  return (
    <>
      <DataState
        state={cadences}
        errorTitle="Couldn't load cadences"
        skeleton={
          <div className={styles.grid}>
            {[0, 1, 2].map((i) => (
              <Card key={i} padding="lg">
                <Skeleton width="55%" height={18} />
                <div style={{ height: "var(--space-3)" }} />
                <Skeleton width="100%" height={48} />
              </Card>
            ))}
          </div>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.WorkflowIcon />}
            title="No cadences yet"
            description="Create a cadence to add timed follow-up touches to your campaigns."
            action={
              <Button iconLeft={<Icons.PlusIcon />} onClick={() => setCreateOpen(true)}>
                New cadence
              </Button>
            }
          />
        }
      >
        {(rows) => (
          <div className={styles.grid}>
            {rows.map((c) => (
              <CadenceCard key={c.id} cadence={c} onDeactivate={() => deactivate(c)} />
            ))}
          </div>
        )}
      </DataState>

      {createOpen && (
        <CreateCadenceModal
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            cadences.refetch();
            setCreateOpen(false);
          }}
          onError={(msg) => toast.error("Couldn't create cadence", msg)}
        />
      )}
    </>
  );
}

function CadenceCard({ cadence, onDeactivate }: { cadence: Cadence; onDeactivate: () => void }) {
  return (
    <Card padding="lg" className={styles.cadence}>
      <div className={styles.cadenceHead}>
        <h3 className={styles.cadenceName}>{cadence.name}</h3>
        <Badge tone={cadence.is_active ? "success" : "neutral"} dot>
          {cadence.is_active ? "Active" : "Inactive"}
        </Badge>
      </div>
      {cadence.description && <p className={styles.cadenceDesc}>{cadence.description}</p>}

      <ol className={styles.steps}>
        {cadence.steps.map((s) => (
          <li key={s.step_index} className={styles.step}>
            <span className={styles.stepNum}>{s.step_index + 1}</span>
            <div className={styles.stepBody}>
              <div className={styles.stepTop}>
                <Badge tone="neutral">{humanize(s.channel)}</Badge>
                <span className={styles.stepDelay}>
                  {s.delay_days === 0 ? "Immediately" : `Day ${cumulativeDay(cadence.steps, s.step_index)}`}
                </span>
              </div>
              {s.angle && <p className={styles.stepAngle}>{s.angle}</p>}
            </div>
          </li>
        ))}
      </ol>

      {cadence.is_active && (
        <div className={styles.cadenceFoot}>
          <Button variant="ghost" size="sm" iconLeft={<Icons.XIcon />} onClick={onDeactivate}>
            Deactivate
          </Button>
        </div>
      )}
    </Card>
  );
}

/** Running day offset for a step (sum of delays up to and including it). */
function cumulativeDay(steps: { delay_days: number }[], index: number): number {
  let day = 0;
  for (let i = 0; i <= index; i++) day += steps[i]?.delay_days ?? 0;
  return day;
}

function CreateCadenceModal({
  onClose,
  onCreated,
  onError,
}: {
  onClose: () => void;
  onCreated: () => void;
  onError: (msg: string) => void;
}) {
  const api = useApiClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [steps, setSteps] = useState<CadenceStepInput[]>([blankStep()]);
  const [submitting, setSubmitting] = useState(false);

  function updateStep(i: number, patch: Partial<CadenceStepInput>) {
    setSteps((prev) => prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));
  }
  function addStep() {
    setSteps((prev) => [...prev, blankStep()]);
  }
  function removeStep(i: number) {
    setSteps((prev) => (prev.length > 1 ? prev.filter((_, idx) => idx !== i) : prev));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const body: CadenceInput = {
        name: name.trim(),
        description: description.trim() || null,
        steps: steps.map((s) => ({
          delay_days: Number(s.delay_days) || 0,
          channel: s.channel || "email",
          angle: s.angle.trim(),
        })),
      };
      await api.createCadence(body);
      onCreated();
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
      title="New cadence"
      description="Each step waits its delay, then sends on its channel using the angle as the AI's brief."
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button form="new-cadence-form" type="submit" loading={submitting} disabled={!name.trim()}>
            Create cadence
          </Button>
        </>
      }
    >
      <form id="new-cadence-form" className={styles.form} onSubmit={onSubmit} noValidate>
        <Field label="Cadence name" required>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Founder-led 4-touch"
            required
          />
        </Field>
        <Field label="Description" hint="Optional. A note for the team on when to use this cadence.">
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="For warm inbound that went quiet"
          />
        </Field>

        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Steps</legend>
          <div className={styles.stepEditors}>
            {steps.map((s, i) => (
              <div key={i} className={styles.stepEditor}>
                <div className={styles.stepEditorHead}>
                  <span className={styles.stepEditorNum}>Step {i + 1}</span>
                  {steps.length > 1 && (
                    <IconButton
                      label={`Remove step ${i + 1}`}
                      size="sm"
                      variant="ghost"
                      icon={<Icons.TrashIcon />}
                      onClick={() => removeStep(i)}
                    />
                  )}
                </div>
                <div className={styles.stepEditorRow}>
                  <Field label="Wait (days)">
                    <Input
                      type="number"
                      min={0}
                      value={String(s.delay_days)}
                      onChange={(e) => updateStep(i, { delay_days: Number(e.target.value) })}
                    />
                  </Field>
                  <Field label="Channel">
                    <Select
                      value={s.channel}
                      onChange={(e) => updateStep(i, { channel: e.target.value })}
                      options={CHANNEL_OPTIONS}
                    />
                  </Field>
                </div>
                <Field label="Angle" hint="What this touch should emphasize. The AI drafts from it.">
                  <Textarea
                    rows={2}
                    value={s.angle}
                    onChange={(e) => updateStep(i, { angle: e.target.value })}
                    placeholder="Reference their recent funding; lead with the integration win."
                  />
                </Field>
              </div>
            ))}
          </div>
          <Button type="button" variant="secondary" size="sm" iconLeft={<Icons.PlusIcon />} onClick={addStep}>
            Add step
          </Button>
        </fieldset>
      </form>
    </Modal>
  );
}

/* ------------------------------------------------------------------ Enrollments tab */

function EnrollmentsTab() {
  const api = useApiClient();
  const campaigns = useApi<Campaign[]>((signal) => api.listCampaigns(signal), []);
  const [campaignId, setCampaignId] = useState("");

  const withCadence = useMemo(
    () => (campaigns.data ?? []).filter((c) => c.cadence_id != null),
    [campaigns.data],
  );

  useEffect(() => {
    if (!campaignId && withCadence.length > 0) setCampaignId(withCadence[0].id);
  }, [withCadence, campaignId]);

  if (campaigns.loading) {
    return <Skeleton width="100%" height={200} />;
  }
  if (withCadence.length === 0) {
    return (
      <EmptyState
        icon={<Icons.WorkflowIcon />}
        title="No cadence campaigns yet"
        description="Attach a cadence when you create a campaign, and its enrollments will show up here."
      />
    );
  }

  return (
    <div className={styles.enrollWrap}>
      <Field label="Campaign" hideLabel>
        <Select
          value={campaignId}
          onChange={(e) => setCampaignId(e.target.value)}
          options={withCadence.map((c) => ({ value: c.id, label: c.name }))}
          className={styles.campaignSelect}
        />
      </Field>
      {campaignId && <CampaignEnrollments key={campaignId} campaignId={campaignId} />}
    </div>
  );
}

function CampaignEnrollments({ campaignId }: { campaignId: string }) {
  const api = useApiClient();
  const report = useApi<CadenceReport>((signal) => api.cadenceReport(campaignId, signal), [campaignId]);
  const enrollments = useApi<CadenceEnrollment[]>(
    (signal) => api.listEnrollments(campaignId, signal),
    [campaignId],
  );

  const refresh = useCallback(() => {
    report.refetch();
    enrollments.refetch();
  }, [report, enrollments]);

  return (
    <div className={styles.enrollStack}>
      <DataState
        state={report}
        errorTitle="Couldn't load the report"
        skeleton={<Skeleton width="100%" height={72} />}
      >
        {(r) => (
          <div className={styles.reportRow}>
            <ReportStat label="Enrolled" value={r.total_enrollments} />
            <ReportStat label="Touches sent" value={r.touches_sent} tone="success" />
            <ReportStat label="Touches skipped" value={r.touches_skipped} />
            <ReportStat label="Active" value={r.by_status.active ?? 0} />
          </div>
        )}
      </DataState>

      <Card padding="lg">
        <CardHeader title="Enrollments" subtitle="Each account's position in the cadence" />
        <DataState
          state={enrollments}
          errorTitle="Couldn't load enrollments"
          skeleton={<Skeleton width="100%" height={120} />}
          isEmpty={(rows) => rows.length === 0}
          empty={
            <EmptyState
              compact
              icon={<Icons.UsersIcon />}
              title="No enrollments yet"
              description="Accounts enroll once the campaign is approved and the send phase runs."
            />
          }
        >
          {(rows) => (
            <ul className={styles.enrollments}>
              {rows.map((e) => (
                <EnrollmentRow key={e.id} enrollment={e} onChanged={refresh} />
              ))}
            </ul>
          )}
        </DataState>
      </Card>
    </div>
  );
}

function ReportStat({ label, value, tone }: { label: string; value: number; tone?: "success" }) {
  return (
    <div className={styles.reportStat}>
      <span className={styles.reportLabel}>{label}</span>
      <span className={styles.reportValue} data-tone={tone}>
        {formatNumber(value)}
      </span>
    </div>
  );
}

function EnrollmentRow({
  enrollment,
  onChanged,
}: {
  enrollment: CadenceEnrollment;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const detail = useApi<EnrollmentDetail>(
    (signal) => (open ? api.getEnrollment(enrollment.id, signal) : Promise.resolve(null as unknown as EnrollmentDetail)),
    [open, enrollment.id],
  );

  const act = useCallback(
    async (fn: () => Promise<unknown>, ok: string) => {
      setBusy(true);
      try {
        await fn();
        toast.success(ok);
        onChanged();
        if (open) detail.refetch();
      } catch (err) {
        toast.error("Action failed", err instanceof ApiError ? err.detail : "Please try again.");
      } finally {
        setBusy(false);
      }
    },
    [toast, onChanged, open, detail],
  );

  const status = enrollment.status;
  return (
    <li className={styles.enrollment}>
      <div className={styles.enrollMain}>
        <button
          type="button"
          className={styles.enrollToggle}
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <span className={styles.chevron} data-open={open} aria-hidden="true">
            <Icons.ChevronRightIcon />
          </span>
          <code className={styles.enrollId}>{enrollment.account_id.slice(0, 8)}</code>
        </button>
        <Badge tone={enrollmentTone(status)} dot>
          {humanize(status)}
        </Badge>
        <span className={styles.enrollStep}>Step {enrollment.current_step_index + 1}</span>
        {enrollment.next_touch_at && status === "active" && (
          <span className={styles.enrollNext}>Next {timeAgo(enrollment.next_touch_at)}</span>
        )}
        {enrollment.stop_reason && (
          <span className={styles.enrollStop}>{humanize(enrollment.stop_reason)}</span>
        )}
        <span className={styles.enrollActions}>
          {status === "active" && (
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => act(() => api.pauseEnrollment(enrollment.id), "Paused")}
            >
              Pause
            </Button>
          )}
          {status === "paused" && (
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => act(() => api.resumeEnrollment(enrollment.id), "Resumed")}
            >
              Resume
            </Button>
          )}
          {(status === "active" || status === "paused") && (
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => act(() => api.stopEnrollment(enrollment.id), "Stopped")}
            >
              Stop
            </Button>
          )}
        </span>
      </div>

      {open && (
        <div className={styles.touches}>
          <DataState
            state={detail}
            errorTitle="Couldn't load touches"
            skeleton={<Skeleton width="100%" height={48} />}
            isEmpty={(d) => d == null || d.touches.length === 0}
            empty={<p className={styles.muted}>No touches recorded yet.</p>}
          >
            {(d) => (
              <ul className={styles.touchList}>
                {d.touches.map((t) => (
                  <li key={t.id} className={styles.touch}>
                    <span className={styles.touchStep}>Step {t.step_index + 1}</span>
                    <Badge tone={touchTone(t.status)} dot>
                      {humanize(t.status)}
                    </Badge>
                    {t.skip_reason && <span className={styles.touchNote}>{humanize(t.skip_reason)}</span>}
                    {t.sent_at && <span className={styles.touchNote}>{timeAgo(t.sent_at)}</span>}
                    {t.status === "awaiting_approval" && (
                      <span className={styles.touchActions}>
                        <Button
                          size="sm"
                          disabled={busy}
                          onClick={() => act(() => api.approveTouch(enrollment.id, t.step_index), "Touch approved")}
                        >
                          Approve
                        </Button>
                        <Button
                          variant="secondary"
                          size="sm"
                          disabled={busy}
                          onClick={() => act(() => api.rejectTouch(enrollment.id, t.step_index), "Touch rejected")}
                        >
                          Reject
                        </Button>
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </DataState>
        </div>
      )}
    </li>
  );
}
