import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button, Card, EmptyState, Field, Icons, Input, Select, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { StepsEditor, blankStep, stepsProblem } from "@/components/engagement/StepsEditor";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ConnectedMailbox, EngagementStep, SequenceTemplate } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Start a campaign: its name, the mailbox it sends from, and its steps (spec §9, step 2).
 *
 * Creating it adds nobody and sends nothing. The campaign page that follows is where people are
 * added, first emails drafted and read, and the campaign launched, in that order.
 */

const CUSTOM = "";

function localInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function CampaignBuilder() {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
  const templates = useApi<SequenceTemplate[]>((s) => api.listSequenceTemplates(s), []);

  const mine = useMemo(
    () => (mailboxes.data ?? []).filter((m) => m.mine && m.status === "connected"),
    [mailboxes.data],
  );
  const [name, setName] = useState("");
  const [mailboxId, setMailboxId] = useState("");
  const [templateId, setTemplateId] = useState(CUSTOM);
  const [steps, setSteps] = useState<EngagementStep[]>([blankStep(true), blankStep()]);
  const [firstSend, setFirstSend] = useState<"on_approval" | "scheduled">("on_approval");
  const [firstSendAt, setFirstSendAt] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 30, 0, 0);
    return localInputValue(d);
  });
  const [timezoneMode, setTimezoneMode] = useState<"contact" | "sdr">("contact");
  const [reviewEvery, setReviewEvery] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!mailboxId && mine.length) setMailboxId(mine[0].id);
  }, [mine, mailboxId]);

  function applyTemplate(id: string) {
    setTemplateId(id);
    const t = (templates.data ?? []).find((x) => x.id === id);
    if (t) setSteps(t.steps.map((s) => ({ ...s })));
  }

  const problem = !name.trim()
    ? "Give the campaign a name."
    : !mailboxId
      ? "Choose the mailbox it sends from."
      : stepsProblem(steps);

  async function create() {
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const campaign = await api.createEngagementCampaign({
        name: name.trim(),
        mailbox_id: mailboxId,
        steps,
        template_id: templateId || null,
        review_every_touch: reviewEvery,
        first_send_mode: firstSend,
        first_send_at: firstSend === "scheduled" ? new Date(firstSendAt).toISOString() : null,
        timezone_mode: timezoneMode,
      });
      toast.success("Campaign created", "Now add the people it goes to.");
      navigate(`/engagement/campaigns/${campaign.id}`, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "The campaign could not be created.");
    } finally {
      setSaving(false);
    }
  }

  if (mailboxes.loading && !mailboxes.data) {
    return <Skeleton width="100%" height={360} />;
  }
  if (mailboxes.data && mine.length === 0) {
    return (
      <div className={styles.page}>
        <PageHeader title="New campaign" />
        <EmptyState
          icon={<Icons.MailIcon />}
          title="Connect your mailbox first"
          description="A campaign sends from your own Gmail or Outlook mailbox, so replies come back to you."
          action={<Link to="/mailboxes" className={styles.buttonLink}>Go to My mailboxes</Link>}
        />
      </div>
    );
  }

  const templateOptions = [
    { value: CUSTOM, label: "Write the steps here" },
    ...(templates.data ?? []).map((t) => ({ value: t.id, label: t.name })),
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="New campaign"
        description="Name it, choose where it sends from, and set its steps. People are added next, and nothing sends until you approve it."
      />

      <form
        className={styles.form}
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
        noValidate
      >
        <Card padding="lg" className={styles.section}>
          <h2 className={styles.sectionTitle}>The campaign</h2>
          <div className={styles.fields}>
            <Field label="Name" required>
              <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={200}
                placeholder="Q4 fintech CFOs" autoFocus />
            </Field>
            <Field label="Sends from" hint="Your own mailbox. Replies come back to it.">
              <Select
                value={mailboxId}
                onChange={(e) => setMailboxId(e.target.value)}
                options={mine.map((m) => ({ value: m.id, label: m.email }))}
              />
            </Field>
          </div>
        </Card>

        <Card padding="lg" className={styles.section}>
          <div className={styles.sectionHead}>
            <h2 className={styles.sectionTitle}>Steps</h2>
            {(templates.data?.length ?? 0) > 0 && (
              <Field label="Start from" hideLabel>
                <Select value={templateId} onChange={(e) => applyTemplate(e.target.value)} options={templateOptions} />
              </Field>
            )}
          </div>
          <StepsEditor steps={steps} onChange={(next) => { setSteps(next); setTemplateId(CUSTOM); }} />
        </Card>

        <Card padding="lg" className={styles.section}>
          <h2 className={styles.sectionTitle}>Timing</h2>
          <div className={styles.fields}>
            <Field label="Opening emails go out" hint="Every opening email is read and approved first either way.">
              <Select
                value={firstSend}
                onChange={(e) => setFirstSend(e.target.value as typeof firstSend)}
                options={[
                  { value: "on_approval", label: "As soon as I approve each one" },
                  { value: "scheduled", label: "At a time I choose" },
                ]}
              />
            </Field>
            {firstSend === "scheduled" && (
              <Field label="Starting at" hint="Your local time.">
                <Input type="datetime-local" value={firstSendAt} onChange={(e) => setFirstSendAt(e.target.value)} />
              </Field>
            )}
            <Field label="Follow the timezone of" hint="Used for every follow-up and set send time.">
              <Select
                value={timezoneMode}
                onChange={(e) => setTimezoneMode(e.target.value as typeof timezoneMode)}
                options={[
                  { value: "contact", label: "Each contact (their working morning)" },
                  { value: "sdr", label: "Me" },
                ]}
              />
            </Field>
          </div>
          <label className={styles.checkRow}>
            <input type="checkbox" checked={reviewEvery} onChange={(e) => setReviewEvery(e.target.checked)} />
            <span>
              Review every follow-up too, not only the opening email.
              <span className={styles.muted}> Each one waits for you before it sends.</span>
            </span>
          </label>
        </Card>

        {error && <p className={styles.formError} role="alert">{error}</p>}
        <div className={styles.formActions}>
          <Button type="button" variant="ghost" onClick={() => navigate("/engagement/campaigns")}>Cancel</Button>
          <Button type="submit" loading={saving} iconRight={<Icons.ChevronRightIcon />}>
            Create and add people
          </Button>
        </div>
      </form>
    </div>
  );
}

export default CampaignBuilder;
