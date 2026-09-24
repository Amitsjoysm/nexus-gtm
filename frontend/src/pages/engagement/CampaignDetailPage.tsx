import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, Card, DataTable, EmptyState, ErrorState, Field, Icons, Input, Modal, Skeleton,
  TabPanel, Tabs,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { BestTimeHint } from "@/components/engagement/BestTimeHint";
import { ContactPicker } from "@/components/engagement/ContactPicker";
import { useContactInsights } from "@/components/engagement/InsightBadge";
import { zonedClockToLocalInput } from "@/components/engagement/zonedTime";
import { StepsEditor, stepsProblem } from "@/components/engagement/StepsEditor";
import {
  CAMPAIGN_STATUS, ENROLLMENT_STATUS, WEEKDAYS, reasonText, when,
} from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type {
  BestTimeSuggestion, ConnectedMailbox, EngagementCampaign, EngagementEnrollment, EngagementStep,
  EnrollResult,
} from "@/lib/types";
import { LaunchPanel } from "./LaunchPanel";
import { ReportsPanel } from "./ReportsPanel";
import { ReviewQueue } from "./ReviewQueue";
import styles from "./Engagement.module.css";

/**
 * One campaign, from setting up to steering (spec §9).
 *
 * Before launch the tabs are the order the work happens in: add People, Review their opening
 * emails, Launch. After launch, People becomes the live view (each person's step, next send and
 * why they stopped) with per-person pause, resume, stop, send now and move. Review stays, because
 * anyone added later is read and approved the same way.
 */

type TabKey = "people" | "results" | "review" | "launch" | "steps";
const SETTING_UP = new Set(["draft", "reviewing"]);

function toLocalInput(iso: string | null): string {
  const d = iso ? new Date(iso) : new Date(Date.now() + 60 * 60 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function CampaignDetailPage() {
  const { campaignId = "" } = useParams();
  const api = useApiClient();
  const toast = useToast();
  const campaign = useApi<EngagementCampaign>((s) => api.getEngagementCampaign(campaignId, s), [campaignId]);
  const enrollments = useApi<EngagementEnrollment[]>(
    (s) => api.campaignEnrollments(campaignId, s), [campaignId],
  );
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
  const [tab, setTab] = useState<TabKey | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [lastAdd, setLastAdd] = useState<EnrollResult | null>(null);

  const c = campaign.data;
  const people = enrollments.data ?? [];
  const enrolledIds = useMemo(() => new Set(people.map((p) => p.contact_id)), [people]);
  const awaiting = c?.counts.awaiting_review ?? 0;
  const settingUp = c ? SETTING_UP.has(c.status) : false;
  const canLaunch = c ? settingUp || c.status === "paused" : false;

  // Open where the work is: people first while there are none, then review, then launch.
  useEffect(() => {
    if (!c || tab !== null) return;
    const count = Object.values(c.counts).reduce((a, b) => a + b, 0);
    setTab(settingUp ? (count === 0 ? "people" : awaiting > 0 ? "review" : "launch") : "people");
  }, [c, tab, settingUp, awaiting]);

  function refresh() {
    campaign.refetch();
    enrollments.refetch();
  }

  async function act(action: "pause" | "stop") {
    setActing(action);
    try {
      await api.campaignAction(campaignId, action);
      toast.success(action === "pause" ? "Campaign paused" : "Campaign stopped",
        action === "pause" ? "Nothing more sends until you resume it." : "Nobody else will be emailed from it.");
      refresh();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setActing(null);
    }
  }

  async function addPeople(ids: string[]) {
    try {
      const result = await api.addCampaignContacts(campaignId, ids);
      setLastAdd(result);
      toast.success(`${result.added.length} added`,
        result.skipped.length ? `${result.skipped.length} could not be added.` : "Next, write and review their opening emails.");
      refresh();
    } catch (err) {
      toast.error("Couldn't add them", err instanceof ApiError ? err.detail : "Please try again.");
    }
  }

  if (campaign.error) {
    return (
      <ErrorState
        title={campaign.error.status === 404 ? "Campaign not found" : "Couldn't load the campaign"}
        message={campaign.error.status === 404 ? "It may belong to someone else, or it was removed." : campaign.error.detail}
        onRetry={campaign.error.status === 404 ? undefined : campaign.refetch}
      />
    );
  }
  if (!c) return <Skeleton width="100%" height={420} />;

  const status = CAMPAIGN_STATUS[c.status] ?? { label: c.status, tone: "neutral" as const };
  const mailbox = (mailboxes.data ?? []).find((m) => m.id === c.mailbox_connection_id);
  const total = Object.values(c.counts).reduce((a, b) => a + b, 0);
  const tabs = [
    { value: "people", label: "People", count: total },
    // Once anything could have gone out, what came of it.
    ...(settingUp ? [] : [{ value: "results", label: "Results" }]),
    { value: "review", label: "Review", count: awaiting },
    ...(canLaunch ? [{ value: "launch", label: c.status === "paused" ? "Resume" : "Launch" }] : []),
    { value: "steps", label: "Steps", count: c.steps.length },
  ];

  return (
    <div className={styles.page}>
      <Link to="/engagement/campaigns" className={styles.back}>
        <Icons.ChevronLeftIcon aria-hidden /> Campaigns
      </Link>
      <PageHeader
        title={c.name}
        description={
          <span className={styles.headerMeta}>
            <Badge tone={status.tone} dot>{status.label}</Badge>
            {c.status === "paused" && c.pause_reason && c.pause_reason !== "manual" && (
              <span>Paused because {reasonText(c.pause_reason)}</span>
            )}
            <span>Sends from {mailbox?.email ?? "your mailbox"}</span>
          </span>
        }
        actions={
          <>
            {c.status === "active" && (
              <Button variant="secondary" loading={acting === "pause"} disabled={acting !== null}
                onClick={() => act("pause")}>Pause</Button>
            )}
            {(c.status === "active" || c.status === "paused") && (
              <Button variant="ghost" loading={acting === "stop"} disabled={acting !== null}
                onClick={() => act("stop")}>Stop</Button>
            )}
          </>
        }
      />

      {c.status === "paused" && c.pause_reason === "out_of_credits" && (
        <p className={styles.notice} role="status">
          It paused when a charge could not be covered. Once credits are added, resume it from the
          Resume tab and it continues from where it stopped.
        </p>
      )}
      {mailbox && mailbox.status !== "connected" && (
        <p className={styles.notice} role="alert">
          {mailbox.email} needs reconnecting before this campaign can send.{" "}
          <Link to="/mailboxes" className={styles.inlineLink}>Reconnect it on My mailboxes</Link>.
        </p>
      )}

      <Tabs items={tabs} value={tab ?? "people"} onChange={(v) => setTab(v as TabKey)} idPrefix="campaign" />

      <TabPanel id="campaign-panel-people" active={(tab ?? "people") === "people"}>
        <div className={styles.stack}>
          {c.status !== "completed" && (
            <Card padding="lg" className={styles.section}>
              <h2 className={styles.sectionTitle}>Add people</h2>
              <ContactPicker enrolledIds={enrolledIds} onAdd={addPeople} />
              {lastAdd && <AddResult result={lastAdd} people={people} />}
            </Card>
          )}
          <PeopleTable
            rows={people} loading={enrollments.loading && !enrollments.data}
            error={enrollments.error?.detail ?? null} onRetry={enrollments.refetch}
            steps={c.steps.length} live={!settingUp} onChanged={refresh}
          />
        </div>
      </TabPanel>

      {!settingUp && (
        <TabPanel id="campaign-panel-results" active={tab === "results"}>
          <ReportsPanel campaignId={c.id} />
        </TabPanel>
      )}

      <TabPanel id="campaign-panel-review" active={tab === "review"}>
        <ReviewQueue campaignId={c.id} onChanged={refresh} />
      </TabPanel>

      {canLaunch && (
        <TabPanel id="campaign-panel-launch" active={tab === "launch"}>
          <Card padding="lg">
            <LaunchPanel campaign={c} onLaunched={() => { refresh(); setTab("people"); }} />
          </Card>
        </TabPanel>
      )}

      <TabPanel id="campaign-panel-steps" active={tab === "steps"}>
        <StepsPanel campaign={c} editable={settingUp} onSaved={refresh} />
      </TabPanel>
    </div>
  );
}

function AddResult({ result, people }: { result: EnrollResult; people: EngagementEnrollment[] }) {
  const name = (id: string) => people.find((p) => p.contact_id === id)?.contact_name || "Someone";
  if (!result.skipped.length && !result.warnings.length) return null;
  const why: Record<string, string> = {
    not_found: "no longer exists",
    already_enrolled: "is already in this campaign",
    no_email: "has no email address",
  };
  return (
    <div className={styles.addResult} role="status">
      {result.warnings.length > 0 && (
        <>
          <p className={styles.subhead}>Already being emailed elsewhere</p>
          <ul className={styles.plainList}>
            {result.warnings.map((w) => (
              <li key={`${w.contact_id}-${w.campaign_id}`}>
                {name(w.contact_id)} is also in {w.campaign_name || "another campaign"}
                {w.owner ? ` (${w.owner})` : ""}. Added anyway; check before approving.
              </li>
            ))}
          </ul>
        </>
      )}
      {result.skipped.length > 0 && (
        <>
          <p className={styles.subhead}>Not added</p>
          <ul className={styles.plainList}>
            {result.skipped.map((s) => (
              <li key={s.contact_id}>
                {name(s.contact_id)} {s.reason.startsWith("do_not_contact") ? "is on the do-not-contact list" : why[s.reason] ?? s.reason}.
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function PeopleTable({
  rows, loading, error, onRetry, steps, live, onChanged,
}: {
  rows: EngagementEnrollment[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  steps: number;
  live: boolean;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [moving, setMoving] = useState<EngagementEnrollment | null>(null);
  const [moveTo, setMoveTo] = useState("");
  // When this person usually replies, in their own time (D26 applied on the server).
  const movingInsight = useContactInsights(moving ? [moving.contact_id] : []).get(moving?.contact_id ?? "");

  async function act(row: EngagementEnrollment, action: "pause" | "resume" | "stop" | "send-now") {
    setBusy(`${row.id}:${action}`);
    try {
      await api.enrollmentAction(row.id, action);
      const said = { pause: "Paused", resume: "Resumed", stop: "Stopped", "send-now": "Sending now" }[action];
      toast.success(`${said}: ${row.contact_name}`, "");
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  async function move() {
    if (!moving) return;
    setBusy(`${moving.id}:move`);
    try {
      await api.moveEnrollment(moving.id, new Date(moveTo).toISOString());
      toast.success(`Moved: ${moving.contact_name}`, `Next step ${when(new Date(moveTo).toISOString())}.`);
      setMoving(null);
      onChanged();
    } catch (err) {
      toast.error("Couldn't move it", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<EngagementEnrollment>[] = [
    {
      key: "person",
      header: "Person",
      sortable: true,
      sortValue: (r) => r.contact_name.toLowerCase(),
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.personName}>{r.contact_name || r.contact_email}</span>
          <span className={styles.muted}>{[r.contact_title, r.account_name].filter(Boolean).join(" · ")}</span>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => {
        const s = ENROLLMENT_STATUS[r.status];
        const why = reasonText(r.status_reason);
        return (
          <span className={styles.statusCell}>
            <Badge tone={s.tone} dot>{s.label}</Badge>
            {why && <span className={styles.muted}>{why}</span>}
          </span>
        );
      },
    },
    {
      key: "step",
      header: "Step",
      align: "right",
      hideOnMobile: true,
      render: (r) => <span className={styles.num}>{Math.min(r.current_step_index + 1, steps)} of {steps}</span>,
    },
    {
      key: "next",
      header: "Next",
      hideOnMobile: true,
      sortable: true,
      sortValue: (r) => r.snoozed_until ?? r.next_action_at ?? "",
      render: (r) => {
        if (r.status === "stopped" || r.status === "completed") return <span className={styles.muted}>Nothing more</span>;
        if (r.status === "snoozed" || r.status_reason === "out_of_office") return `Back ${when(r.snoozed_until)}`;
        if (r.status === "awaiting_review") return <span className={styles.muted}>After approval</span>;
        return when(r.next_action_at);
      },
    },
  ];
  if (live) {
    columns.push({
      key: "actions",
      header: <span className={styles.srOnly}>Actions</span>,
      align: "right",
      render: (r) => {
        const b = (a: string) => busy === `${r.id}:${a}`;
        const disabled = busy !== null;
        return (
          <div className={styles.rowActions}>
            {r.status === "active" && (
              <>
                <Button size="sm" variant="ghost" loading={b("send-now")} disabled={disabled}
                  onClick={() => act(r, "send-now")}>Send now</Button>
                <Button size="sm" variant="ghost" disabled={disabled}
                  onClick={() => { setMoving(r); setMoveTo(toLocalInput(r.next_action_at)); }}>Move</Button>
                <Button size="sm" variant="ghost" loading={b("pause")} disabled={disabled}
                  onClick={() => act(r, "pause")}>Pause</Button>
              </>
            )}
            {(r.status === "paused" || r.status === "snoozed") && (
              <Button size="sm" variant="ghost" loading={b("resume")} disabled={disabled}
                onClick={() => act(r, "resume")}>Resume</Button>
            )}
            {!["stopped", "completed", "awaiting_review"].includes(r.status) && (
              <Button size="sm" variant="ghost" loading={b("stop")} disabled={disabled}
                onClick={() => act(r, "stop")}>Stop</Button>
            )}
          </div>
        );
      },
    });
  }

  if (error) return <ErrorState title="Couldn't load the people in this campaign" message={error} onRetry={onRetry} />;
  return (
    <>
      <DataTable
        columns={columns}
        rows={rows}
        getRowKey={(r) => r.id}
        loading={loading}
        caption="People in this campaign"
        empty={
          <EmptyState compact icon={<Icons.UsersIcon />} title="Nobody added yet"
            description="Choose people above: from a saved list, by title and seniority, or by search." />
        }
      />
      <Modal
        open={moving !== null}
        onClose={() => setMoving(null)}
        title={`Move ${moving?.contact_name ?? ""}'s next step`}
        description="The next step goes at this time instead of when its timing says. Later steps follow from it."
        footer={
          <>
            <Button variant="ghost" onClick={() => setMoving(null)}>Cancel</Button>
            <Button onClick={move} loading={moving !== null && busy === `${moving.id}:move`} disabled={!moveTo}>
              Move it
            </Button>
          </>
        }
      >
        <Field label="Send the next step at" hint="Your local time.">
          <Input type="datetime-local" value={moveTo} onChange={(e) => setMoveTo(e.target.value)} />
        </Field>
        {moving && movingInsight && movingInsight.best_time.source !== "default" && (
          <BestTimeHint
            suggestion={movingInsight.best_time}
            onUse={(clock) => setMoveTo(zonedClockToLocalInput(
              moveTo.slice(0, 10), clock, moving.contact_timezone))}
          />
        )}
      </Modal>
    </>
  );
}

function StepsPanel({ campaign, editable, onSaved }: {
  campaign: EngagementCampaign;
  editable: boolean;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [steps, setSteps] = useState<EngagementStep[]>(() => campaign.steps.map((s) => ({ ...s })));
  const [saving, setSaving] = useState(false);
  // Only while the steps can still change: a launched campaign's set times are fixed.
  const best = useApi<BestTimeSuggestion | null>(
    (s) => (editable ? api.campaignBestTime(campaign.id, s) : Promise.resolve(null)),
    [campaign.id, editable],
  );
  const problem = stepsProblem(steps);
  const dirty = JSON.stringify(steps) !== JSON.stringify(campaign.steps);

  async function save() {
    setSaving(true);
    try {
      await api.replaceCampaignSteps(campaign.id, steps);
      toast.success("Steps saved", "Drafts already written keep their text; regenerate any you want rewritten.");
      onSaved();
    } catch (err) {
      toast.error("Steps not saved", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  if (!editable) {
    return (
      <Card padding="lg">
        <ol className={styles.stepList}>
          {campaign.steps.map((s, i) => (
            <li key={i} className={styles.stepRow}>
              <span className={styles.stepNumber} aria-hidden="true">{i + 1}</span>
              <div>
                <p className={styles.personName}>
                  {i === 0 ? "Opening email" : s.channel === "call" ? "Call" : "Follow-up email"}
                  {i > 0 && <span className={styles.muted}> · after {s.delay_business_days} business {s.delay_business_days === 1 ? "day" : "days"}</span>}
                  {s.timing_mode === "manual" && s.send_time_local && <span className={styles.muted}> · at {s.send_time_local}</span>}
                </p>
                {s.angle && <p className={styles.muted}>{s.angle}</p>}
                <p className={styles.muted}>{s.allowed_weekdays.map((d) => WEEKDAYS[d]).join(", ")}</p>
              </div>
            </li>
          ))}
        </ol>
        <p className={styles.muted}>Steps are fixed once a campaign has launched.</p>
      </Card>
    );
  }
  return (
    <Card padding="lg" className={styles.section}>
      <StepsEditor steps={steps} onChange={setSteps} bestTime={best.data ?? undefined} />
      {problem && dirty && <p className={styles.formError} role="alert">{problem}</p>}
      <div className={styles.formActions}>
        <Button onClick={save} loading={saving} disabled={!dirty || Boolean(problem)}>Save steps</Button>
      </div>
    </Card>
  );
}

export default CampaignDetailPage;
