import { Button, Field, IconButton, Icons, Input, Select, Textarea } from "@/components/ui";
import type { BestTimeSuggestion, EngagementStep } from "@/lib/types";
import { BestTimeHint } from "./BestTimeHint";
import { WEEKDAYS, stepLabel } from "./labels";
import styles from "./StepsEditor.module.css";

/**
 * The touches of a sequence, in order (spec §8). Used by the campaign builder and by sequence
 * templates, so a template and a campaign are edited with the same words and the same limits.
 *
 * The limits mirror `validate_steps` on the server, which stays the authority: at most 12 steps, at
 * least one of them an email, the first goes when each person's opening email is approved (so it has
 * no delay), later steps wait 0–60 business days, and a set send time is required only when "at a
 * set time" is chosen. Any step may be a call, the first included (SDR round 2, W3.1): the opening
 * email is then the first email step, wherever it sits.
 */

export const MAX_STEPS = 12;

export function blankStep(first = false): EngagementStep {
  return {
    channel: "email",
    angle: "",
    timing_mode: "auto",
    delay_business_days: first ? 0 : 2,
    send_time_local: null,
    allowed_weekdays: [0, 1, 2, 3, 4],
  };
}

/** Why the steps cannot be saved as they are, or `null`. Mirrors the server's checks. */
export function stepsProblem(steps: EngagementStep[]): string | null {
  if (steps.length === 0) return "Add at least one step.";
  if (steps.length > MAX_STEPS) return `A sequence can have at most ${MAX_STEPS} steps.`;
  if (!steps.some((s) => s.channel === "email")) return "Add at least one email step.";
  for (const [i, s] of steps.entries()) {
    if (i > 0 && (s.delay_business_days < 0 || s.delay_business_days > 60)) {
      return `Step ${i + 1}: wait between 0 and 60 business days.`;
    }
    if (s.timing_mode === "manual" && !/^([01]\d|2[0-3]):[0-5]\d$/.test(s.send_time_local ?? "")) {
      return `Step ${i + 1}: choose the time it should go out.`;
    }
    if (s.allowed_weekdays.length === 0) return `Step ${i + 1}: allow at least one day.`;
  }
  return null;
}

export interface StepsEditorProps {
  steps: EngagementStep[];
  onChange: (steps: EngagementStep[]) => void;
  disabled?: boolean;
  /** When the people are known (a campaign, not a template), when most of them reply. */
  bestTime?: BestTimeSuggestion;
}

export function StepsEditor({ steps, onChange, disabled = false, bestTime }: StepsEditorProps) {
  function update(index: number, patch: Partial<EngagementStep>) {
    onChange(steps.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }
  function move(index: number, by: -1 | 1) {
    const target = index + by;
    if (target < 0 || target >= steps.length) return;
    const next = [...steps];
    [next[index], next[target]] = [next[target], next[index]];
    // Whatever lands first goes when the opening email is approved, with no wait. It keeps its
    // channel: a call may lead.
    next[0] = { ...next[0], delay_business_days: 0 };
    onChange(next);
  }
  function remove(index: number) {
    const next = steps.filter((_, i) => i !== index);
    if (next.length) next[0] = { ...next[0], delay_business_days: 0 };
    onChange(next);
  }
  function toggleDay(index: number, day: number) {
    const current = steps[index].allowed_weekdays;
    const next = current.includes(day) ? current.filter((d) => d !== day) : [...current, day];
    update(index, { allowed_weekdays: next.sort((a, b) => a - b) });
  }

  return (
    <div className={styles.editor}>
      <ol className={styles.steps}>
        {steps.map((step, index) => {
          const first = index === 0;
          const label = `Step ${index + 1}`;
          return (
            <li key={index} className={styles.step}>
              <fieldset className={styles.fieldset} disabled={disabled}>
                <legend className={styles.legend}>
                  <span className={styles.number} aria-hidden="true">{index + 1}</span>
                  <span className={styles.srOnly}>{label}: </span>
                  {stepLabel(steps, index)}
                </legend>
                <div className={styles.reorder}>
                  <IconButton
                    label={`Move ${label} up`} size="sm" variant="ghost"
                    onClick={() => move(index, -1)} disabled={disabled || first}
                    icon={<Icons.ChevronLeftIcon className={styles.up} />}
                  />
                  <IconButton
                    label={`Move ${label} down`} size="sm" variant="ghost"
                    onClick={() => move(index, 1)} disabled={disabled || index === steps.length - 1}
                    icon={<Icons.ChevronRightIcon className={styles.down} />}
                  />
                  <IconButton
                    label={`Remove ${label}`} size="sm" variant="ghost"
                    onClick={() => remove(index)} disabled={disabled || steps.length === 1}
                    icon={<Icons.TrashIcon />}
                  />
                </div>

                <div className={styles.grid}>
                  <Field label="Channel">
                    <Select
                      value={step.channel}
                      onChange={(e) => update(index, { channel: e.target.value as EngagementStep["channel"] })}
                      options={[
                        { value: "email", label: "Email" },
                        { value: "call", label: "Call (a task for you)" },
                      ]}
                    />
                  </Field>
                  {!first && (
                    <Field label="Wait" hint="Business days after the previous step.">
                      <Input
                        type="number" inputMode="numeric" min={0} max={60}
                        value={step.delay_business_days}
                        onChange={(e) => update(index, {
                          delay_business_days: Math.max(0, Math.min(60, Number(e.target.value) || 0)),
                        })}
                      />
                    </Field>
                  )}
                  <Field
                    label="When"
                    hint={first
                      ? step.channel === "call"
                        ? "The call joins your call list when you approve the person's opening email, or at the time you schedule."
                        : "Opening emails go when you approve them, or at the time you schedule."
                      : "The best time is the contact's working morning."}
                  >
                    <Select
                      value={step.timing_mode}
                      onChange={(e) => {
                        const mode = e.target.value as EngagementStep["timing_mode"];
                        update(index, {
                          timing_mode: mode,
                          send_time_local: mode === "manual" ? step.send_time_local ?? "09:30" : null,
                        });
                      }}
                      options={[
                        { value: "auto", label: "At the best time" },
                        { value: "manual", label: "At a set time" },
                      ]}
                    />
                  </Field>
                  {step.timing_mode === "manual" && (
                    <Field label="Send at" hint="In the contact's own timezone.">
                      <Input
                        type="time" value={step.send_time_local ?? ""}
                        onChange={(e) => update(index, { send_time_local: e.target.value || null })}
                      />
                    </Field>
                  )}
                </div>

                {step.timing_mode === "manual" && (
                  <BestTimeHint suggestion={bestTime} current={step.send_time_local}
                    onUse={(clock) => update(index, { send_time_local: clock })} />
                )}

                {step.channel === "email" ? (
                  <Field
                    label="What this email is about"
                    hint="The AI writes from this and from what it knows about the person. Leave it blank to let it choose the angle."
                  >
                    <Textarea
                      rows={2} maxLength={500} value={step.angle}
                      placeholder={stepLabel(steps, index) === "Opening email" ? "Open on their recent funding round" : "A different reason to talk"}
                      onChange={(e) => update(index, { angle: e.target.value })}
                    />
                  </Field>
                ) : (
                  <Field
                    label="What this call is about"
                    hint="Shown on the call in your list. Leave it blank to show the step and campaign."
                  >
                    <Textarea
                      rows={2} maxLength={500} value={step.angle}
                      placeholder="Introduce yourself before the first email"
                      onChange={(e) => update(index, { angle: e.target.value })}
                    />
                  </Field>
                )}

                <div className={styles.days} role="group" aria-label={`${label}: days it may go out`}>
                  <span className={styles.daysLabel}>Only on</span>
                  {WEEKDAYS.map((day, d) => {
                    const on = step.allowed_weekdays.includes(d);
                    return (
                      <button
                        key={day} type="button" className={styles.day} aria-pressed={on}
                        onClick={() => toggleDay(index, d)} disabled={disabled}
                      >
                        {day}
                      </button>
                    );
                  })}
                </div>
              </fieldset>
            </li>
          );
        })}
      </ol>
      <Button
        variant="secondary" iconLeft={<Icons.PlusIcon />}
        onClick={() => onChange([...steps, blankStep(steps.length === 0)])}
        disabled={disabled || steps.length >= MAX_STEPS}
      >
        Add a step
      </Button>
    </div>
  );
}
