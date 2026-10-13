import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button, Card, ErrorState, Field, Input, Skeleton, SwitchRow } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { WorkspaceEngagementSettings } from "@/lib/types";
import styles from "@/pages/engagement/Engagement.module.css";

/**
 * Settings > Replies: how replies are handled for the whole workspace (spec §6, D23).
 *
 * Two switches decide whether anything happens without a person (2026-10-13): acting on a clear
 * "not now / no thanks / unsubscribe", and the AI sending its own answer to an interested buyer.
 * A manager ALLOWS each one here; each SDR then chooses for their own mailbox in Settings > My
 * mailboxes (`engagement/settings.reply_automation` combines the two, and the server enforces it).
 * Switches apply the moment they are flipped; the numbers below them save with the button.
 *
 * Managers and up change these; everyone can read them. The limits are the server's
 * (`engagement/settings.validate_update`), repeated here only so the form can say so before saving.
 */

const HARD_MIN = 0.5;
const HARD_MAX = 0.99;

type NumberKey =
  | "reply_confidence_default"
  | "reply_confidence_min"
  | "reply_confidence_max"
  | "reply_reminder_business_hours"
  | "ooo_default_days";
type Form = Record<NumberKey, string>;
type SwitchKey = "auto_actions_allowed" | "auto_send_allowed";

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
  const [switching, setSwitching] = useState<SwitchKey | null>(null);

  // Filled once. A switch saves on its own and must not wipe numbers someone is still typing.
  useEffect(() => {
    if (settings.data && form === null) setForm(toForm(settings.data));
  }, [settings.data, form]);

  if (settings.error) {
    return <ErrorState title="Couldn't load reply settings" message={settings.error.detail} onRetry={settings.refetch} />;
  }
  if (!settings.data || !form) return <Skeleton width="100%" height={360} />;

  const data = settings.data;
  const editable = data.can_edit;
  const problem = problemWith(form);
  const dirty = JSON.stringify(form) !== JSON.stringify(toForm(data));
  const set = (key: NumberKey) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  async function flip(key: SwitchKey, next: boolean) {
    setSwitching(key);
    try {
      settings.setData(await api.updateEngagementSettings({ [key]: next }));
      toast.success(
        next ? "Allowed for the workspace" : "Switched off for the workspace",
        next ? "Each SDR can now turn it on for their own mailbox." : "No mailbox does this until it is allowed again.",
      );
    } catch (err) {
      toast.error("Not changed", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSwitching(null);
    }
  }

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
      setForm(toForm(next));
      toast.success("Reply settings saved", "They apply to the next reply that arrives.");
    } catch (err) {
      toast.error("Not saved", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={styles.page}>
      <PageHeader
        title="Replies"
        description="What happens when a buyer replies: what the AI may do without anyone, and when to remind the team."
      />

      <Card padding="lg" className={styles.section}>
        <div className={styles.cardIntro}>
          <h4 className={styles.sectionTitle}>What the AI may do without you</h4>
          <p className={styles.muted}>
            Allowed here for the whole workspace. Each SDR then turns each one on or off for their
            own mailbox in <Link to="/settings/mailboxes" className={styles.inlineLink}>My mailboxes</Link>.
          </p>
        </div>
        <SwitchRow
          label="Act on clear replies by itself"
          hint={'A confident "not now", "no thanks" or "unsubscribe" snoozes or stops the sequence straight away. When off, the sequence pauses and the reply waits in Replies for a person. Out-of-office replies and bounces are always handled; they only pause or stop sending.'}
          checked={data.auto_actions_allowed}
          disabled={!editable || switching !== null}
          onChange={(next) => flip("auto_actions_allowed", next)}
        />
        <SwitchRow
          label="Let the AI send replies"
          hint="The AI emails its answer to a buyer who is interested or asked a question, only when it is confident and its draft passes every check. It never offers meeting times, and answers once per conversation; after that, replies come to a person."
          status={data.auto_send_allowed
            ? "Allowed. Nothing is sent by itself until an SDR turns it on for their mailbox."
            : undefined}
          checked={data.auto_send_allowed}
          disabled={!editable || switching !== null}
          onChange={(next) => flip("auto_send_allowed", next)}
        />
      </Card>

      <Card padding="lg" className={styles.section}>
        <div className={styles.cardIntro}>
          <h4 className={styles.sectionTitle}>Confidence bar</h4>
          <p className={styles.muted}>
            How sure the AI must be before it acts on a reply or answers it. Below the bar, the reply
            waits in Replies for a person. Each SDR can set their own bar for their mailbox, inside
            the range below.
          </p>
        </div>
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
        <h4 className={styles.sectionTitle}>Timing</h4>
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
            <Button variant="ghost" disabled={!dirty || saving} onClick={() => setForm(toForm(data))}>
              Discard changes
            </Button>
            <Button onClick={save} loading={saving} disabled={!dirty || Boolean(problem)}>Save settings</Button>
          </div>
        </>
      ) : (
        <p className={styles.muted}>Only managers and above can change these.</p>
      )}
    </div>
  );
}

export default EngagementSettingsPage;
