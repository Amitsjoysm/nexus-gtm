import { useState } from "react";
import { Badge, Button, Card, CardHeader, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { cn } from "@/lib/cn";
import type { FeatureSwitchList, FeatureSwitchRow, FeatureSwitchState } from "@/lib/types";
import styles from "./FeatureSwitchesTab.module.css";

/**
 * Take a feature offline for every workspace, with a message, without a deploy.
 *
 * Keyed on the `module.*` capability ids the product already uses for the nav item, the route
 * guard and the endpoints behind them, so a switch reaches all three and covers pages shipped
 * later for free.
 *
 * This is the most destructive screen in the admin console: one click removes a product surface
 * from every paying customer at once. So it is deliberately slower than the rest — a state is
 * chosen, a message typed, and a Save pressed, rather than a toggle that acts on the way past.
 */

const STATE_COPY: Record<FeatureSwitchState, { label: string; hint: string }> = {
  enabled: { label: "On", hint: "Working normally for everyone." },
  coming_soon: {
    label: "Coming soon",
    hint: "Visible in the sidebar, marked Soon. Use for a feature that is not built yet.",
  },
  maintenance: {
    label: "Maintenance",
    hint: "Off temporarily. Says we are working on it and that data is untouched.",
  },
  disabled: {
    label: "Off",
    hint: "Off with no timeline. Says to contact support.",
  },
};

/** Ceiling, mirrored from `MESSAGE_MAX_CHARS` in `api/routers/admin_features.py`. */
const MESSAGE_MAX = 600;

/**
 * What to write, per state. The three blocking states need genuinely different messages, and a
 * single "Message to customers" label with one placeholder got one kind: a status line.
 *
 * `coming_soon` is the odd one out and the reason this exists. It is not an outage notice — it is
 * the only place in the product where we get to tell a rep what is arriving and why they should
 * care. "Relationship graph lands in October" is a date; what a rep can act on is the job it does
 * for them. So the prompt asks for that explicitly.
 */
const MESSAGE_COPY: Record<
  Exclude<FeatureSwitchState, "enabled">,
  { label: string; placeholder: string; hint: string }
> = {
  coming_soon: {
    label: "What is coming, and why it matters",
    placeholder: [
      "Warm intro paths, landing in October.",
      "",
      "See which of your colleagues already knows someone at an account before you call it, so " +
        "you can open with a referral instead of a cold touch.",
    ].join("\n"),
    hint:
      "Say what it does and what it saves the rep. This is the only place we get to sell a " +
      "feature that has not shipped, so a date on its own is a wasted banner. Line breaks are " +
      "kept, so lead with one line and explain underneath.",
  },
  maintenance: {
    label: "What to tell customers while it is down",
    placeholder: "Back at 14:00 UTC. Your data is untouched.",
    hint:
      "A time and a reassurance. This is the sentence a rep will repeat to a prospect who asks " +
      "why the call did not go through.",
  },
  disabled: {
    label: "Why this is off",
    placeholder: "Retired in favour of the new Sequences builder. Contact support if you relied on it.",
    hint: "No timeline implied. Say what replaced it, or who to talk to.",
  },
};

const TONE: Record<FeatureSwitchState, "success" | "warning" | "info" | "neutral"> = {
  enabled: "success",
  coming_soon: "info",
  maintenance: "warning",
  disabled: "neutral",
};

function FeatureRow({
  row,
  states,
  onSaved,
}: {
  row: FeatureSwitchRow;
  states: FeatureSwitchState[];
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<FeatureSwitchState>(row.state);
  const [message, setMessage] = useState(row.message);
  const [saving, setSaving] = useState(false);

  const dirty = state !== row.state || message !== row.message;
  const fieldId = `fs-${row.capability_id.replace(/\W/g, "")}`;

  async function save() {
    setSaving(true);
    try {
      await api.adminSetFeature(row.capability_id, { state, message });
      toast.success(
        state === "enabled"
          ? `${row.name} is back on`
          : `${row.name} is now ${STATE_COPY[state].label.toLowerCase()}`,
        // The TTL is a real delay an operator has to know about, or they refresh a customer's
        // browser, see the feature still working, and conclude the switch is broken.
        "Live within 30 seconds across the API and the worker.",
      );
      setOpen(false);
      onSaved();
    } catch (err) {
      toast.error(
        `Couldn't change ${row.name}`,
        err instanceof Error ? err.message : "Please try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  function cancel() {
    setState(row.state);
    setMessage(row.message);
    setOpen(false);
  }

  return (
    <li className={cn(styles.row, row.state !== "enabled" && styles.rowOff)}>
      <div className={styles.rowMain}>
        <div className={styles.rowIdent}>
          <span className={styles.rowName}>{row.name}</span>
          <code className={styles.rowId}>{row.capability_id}</code>
        </div>

        <div className={styles.rowStatus}>
          <Badge tone={TONE[row.state]} dot>
            {STATE_COPY[row.state].label}
          </Badge>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => (open ? cancel() : setOpen(true))}
            aria-expanded={open}
            aria-controls={`${fieldId}-panel`}
          >
            {open ? "Cancel" : "Change"}
          </Button>
        </div>
      </div>

      {row.message && !open && <p className={styles.rowMessage}>“{row.message}”</p>}

      {/* What else this takes down, straight from `depends_on` rather than a list kept beside it.
          Shown always, not only while editing: the question "what does switching this off actually
          stop?" is the one an operator has mid-incident, before they open anything. */}
      {row.gates.length > 0 && (
        <p className={styles.rowGates}>
          Also stops {row.gates.length} {row.gates.length === 1 ? "capability" : "capabilities"}:{" "}
          <span className={styles.rowGateList}>{row.gates.join(", ")}</span>
        </p>
      )}

      {open && (
        <div className={styles.panel} id={`${fieldId}-panel`}>
          <fieldset className={styles.states}>
            <legend className={styles.legend}>State</legend>
            {states.map((s) => (
              <label key={s} className={cn(styles.state, state === s && styles.stateActive)}>
                <input
                  type="radio"
                  name={fieldId}
                  value={s}
                  checked={state === s}
                  onChange={() => setState(s)}
                  className={styles.radio}
                />
                <span className={styles.stateLabel}>{STATE_COPY[s].label}</span>
                <span className={styles.stateHint}>{STATE_COPY[s].hint}</span>
              </label>
            ))}
          </fieldset>

          {state !== "enabled" && (
            <div className={styles.field}>
              <label htmlFor={`${fieldId}-msg`} className={styles.label}>
                {MESSAGE_COPY[state].label}
              </label>
              <textarea
                id={`${fieldId}-msg`}
                className={styles.textarea}
                // `coming_soon` is the one state you write a pitch in rather than a status line,
                // so it opens with room for one. A two-row box is an instruction to be terse.
                rows={state === "coming_soon" ? 6 : 3}
                value={message}
                maxLength={MESSAGE_MAX}
                onChange={(e) => setMessage(e.target.value)}
                placeholder={MESSAGE_COPY[state].placeholder}
                aria-describedby={`${fieldId}-hint`}
              />
              <div className={styles.fieldFoot}>
                <p id={`${fieldId}-hint`} className={styles.hint}>
                  {MESSAGE_COPY[state].hint}
                </p>
                {/* Only once it matters. A counter sitting at 0/600 from the first keystroke is
                    noise; near the ceiling it is the one thing you need to see. */}
                {message.length > MESSAGE_MAX * 0.75 && (
                  <span
                    className={cn(
                      styles.counter,
                      message.length >= MESSAGE_MAX && styles.counterFull,
                    )}
                    aria-live="polite"
                  >
                    {message.length} / {MESSAGE_MAX}
                  </span>
                )}
              </div>
            </div>
          )}

          {state === "enabled" && row.state !== "enabled" && (
            <p className={styles.hint}>
              Turning this back on clears the message. A stale notice on a working feature is worse
              than none, because somebody will believe it.
            </p>
          )}

          <div className={styles.actions}>
            <Button onClick={save} disabled={!dirty || saving} loading={saving}>
              {state === "enabled" ? "Turn on" : "Save change"}
            </Button>
            <Button variant="ghost" onClick={cancel} disabled={saving}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

export function FeatureSwitchesTab() {
  const api = useApiClient();
  const list = useApi<FeatureSwitchList>((signal) => api.adminFeatures(signal), []);

  return (
    <Card padding="lg">
      <CardHeader
        title="Feature switches"
        subtitle="Take a module offline for every workspace, with a message. Takes effect within 30 seconds; no deploy."
      />
      <DataState
        state={list}
        errorTitle="Couldn't load feature switches"
        skeleton={
          <div className={styles.rows}>
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} width="100%" height={56} />
            ))}
          </div>
        }
      >
        {(data) => {
          const off = data.features.filter((f) => f.state !== "enabled");
          return (
            <>
              {/* Only when something is off. A permanent "all systems normal" strip trains people
                  to skip the space, and then the line that matters lands where nobody looks. */}
              {off.length > 0 && (
                <p className={styles.summary} role="status">
                  {off.length} {off.length === 1 ? "module is" : "modules are"} switched off for
                  every workspace: {off.map((f) => f.name).join(", ")}.
                </p>
              )}
              <ul className={styles.rows}>
                {data.features.map((row) => (
                  <FeatureRow
                    key={row.capability_id}
                    row={row}
                    states={data.states}
                    onSaved={list.refetch}
                  />
                ))}
              </ul>
            </>
          );
        }}
      </DataState>
    </Card>
  );
}

export default FeatureSwitchesTab;
