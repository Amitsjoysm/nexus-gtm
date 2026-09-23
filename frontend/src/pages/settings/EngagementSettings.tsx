import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button, Card, ErrorState, Field, Icons, Input, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { WorkspaceEngagementSettings } from "@/lib/types";
import { TrainingConsentCard } from "./TrainingConsentCard";
import styles from "@/pages/engagement/Engagement.module.css";

/**
 * How replies are handled for the whole workspace (spec §6, D23): the confidence bar, the range an
 * SDR may choose inside, how long an interested buyer may wait before a reminder, and how long an
 * out-of-office with no return date pauses a sequence.
 *
 * Managers and up change these; everyone can read them. It is its own page rather than a card in
 * Settings because Settings is admin-only and this is a team lead's decision. The limits are the
 * server's (`engagement/settings.validate_update`), repeated here only so the form can say so
 * before saving.
 */

const HARD_MIN = 0.5;
const HARD_MAX = 0.99;

type Form = Record<keyof Omit<WorkspaceEngagementSettings, "can_edit">, string>;

function toForm(s: WorkspaceEngagementSettings): Form {
  return {
    reply_confidence_default: s.reply_confidence_default.toFixed(2),
    reply_confidence_min: s.reply_confidence_min.toFixed(2),
    reply_confidence_max: s.reply_confidence_max.toFixed(2),
    reply_reminder_business_hours: String(s.reply_reminder_business_hours),
    ooo_default_days: String(s.ooo_default_days),
  };
}

function problemWith(f: Form): string | null {
  const [def, min, max] = [f.reply_confidence_default, f.reply_confidence_min, f.reply_confidence_max].map(Number);
  if ([def, min, max].some((n) => Number.isNaN(n) || n < HARD_MIN || n > HARD_MAX)) {
    return `Confidence values run from ${HARD_MIN.toFixed(2)} to ${HARD_MAX.toFixed(2)}.`;
  }
  if (min > max) return "The lowest value SDRs may choose cannot be above the highest.";
  if (def < min || def > max) return "The workspace default must sit inside the range SDRs may choose from.";
  const hours = Number(f.reply_reminder_business_hours);
  if (!Number.isInteger(hours) || hours < 1 || hours > 72) return "The reminder is between 1 and 72 business hours.";
  const days = Number(f.ooo_default_days);
  if (!Number.isInteger(days) || days < 1 || days > 60) return "Out of office pauses for 1 to 60 days.";
  return null;
}

export function EngagementSettingsPage() {
  const api = useApiClient();
  const toast = useToast();
  const settings = useApi<WorkspaceEngagementSettings>((s) => api.engagementSettings(s), []);
  const [form, setForm] = useState<Form | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings.data) setForm(toForm(settings.data));
  }, [settings.data]);

  if (settings.error) {
    return <ErrorState title="Couldn't load reply settings" message={settings.error.detail} onRetry={settings.refetch} />;
  }
  if (!settings.data || !form) return <Skeleton width="100%" height={360} />;

  const editable = settings.data.can_edit;
  const problem = problemWith(form);
  const dirty = JSON.stringify(form) !== JSON.stringify(toForm(settings.data));
  const set = (key: keyof Form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  async function save() {
    if (!form || problem) return;
    setSaving(true);
    try {
      const next = await api.updateEngagementSettings({
        reply_confidence_default: Number(form.reply_confidence_default),
        reply_confidence_min: Number(form.reply_confidence_min),
        reply_confidence_max: Number(form.reply_confidence_max),
        reply_reminder_business_hours: Number(form.reply_reminder_business_hours),
        ooo_default_days: Number(form.ooo_default_days),
      });
      settings.setData(next);
      toast.success("Reply settings saved", "They apply to the next reply that arrives.");
    } catch (err) {
      toast.error("Not saved", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={styles.page}>
      <Link to="/engagement/replies" className={styles.back}>
        <Icons.ChevronLeftIcon aria-hidden /> Replies
      </Link>
      <PageHeader
        title="Reply settings"
        description="How sure the AI must be before it acts on a reply without anyone, and when to remind the team."
      />

      <Card padding="lg" className={styles.section}>
        <h2 className={styles.sectionTitle}>Acting on replies without you</h2>
        <p className={styles.muted}>
          A clear "not now", "no thanks" or "unsubscribe" read at or above the bar is acted on
          straight away: the sequence snoozes or stops. Below it, the reply waits in Replies for a
          person. Each SDR can set their own bar for their mailbox, inside the range below.
        </p>
        <fieldset className={`${styles.fields} ${styles.bare}`} disabled={!editable}>
          <Field label="Workspace default" hint="Used when an SDR has not set their own.">
            <Input type="number" inputMode="decimal" step="0.01" min={HARD_MIN} max={HARD_MAX}
              value={form.reply_confidence_default} onChange={set("reply_confidence_default")} />
          </Field>
          <Field label="Lowest an SDR may choose" hint={`Never below ${HARD_MIN.toFixed(2)}.`}>
            <Input type="number" inputMode="decimal" step="0.01" min={HARD_MIN} max={HARD_MAX}
              value={form.reply_confidence_min} onChange={set("reply_confidence_min")} />
          </Field>
          <Field label="Highest an SDR may choose" hint={`Never above ${HARD_MAX.toFixed(2)}.`}>
            <Input type="number" inputMode="decimal" step="0.01" min={HARD_MIN} max={HARD_MAX}
              value={form.reply_confidence_max} onChange={set("reply_confidence_max")} />
          </Field>
        </fieldset>
      </Card>

      <Card padding="lg" className={styles.section}>
        <h2 className={styles.sectionTitle}>Timing</h2>
        <fieldset className={`${styles.fields} ${styles.bare}`} disabled={!editable}>
          <Field label="Remind after" hint="Business hours an interested buyer or a question may wait for an answer.">
            <Input type="number" inputMode="numeric" min={1} max={72}
              value={form.reply_reminder_business_hours} onChange={set("reply_reminder_business_hours")} />
          </Field>
          <Field label="Out of office with no return date" hint="Days the sequence pauses before it resumes.">
            <Input type="number" inputMode="numeric" min={1} max={60}
              value={form.ooo_default_days} onChange={set("ooo_default_days")} />
          </Field>
        </fieldset>
      </Card>

      {editable ? (
        <>
          {problem && dirty && <p className={styles.formError} role="alert">{problem}</p>}
          <div className={styles.formActions}>
            <Button variant="ghost" disabled={!dirty || saving} onClick={() => setForm(toForm(settings.data as WorkspaceEngagementSettings))}>
              Discard changes
            </Button>
            <Button onClick={save} loading={saving} disabled={!dirty || Boolean(problem)}>Save settings</Button>
          </div>
        </>
      ) : (
        <p className={styles.muted}>Only managers and above can change these.</p>
      )}

      <TrainingConsentCard />
    </div>
  );
}

export default EngagementSettingsPage;
