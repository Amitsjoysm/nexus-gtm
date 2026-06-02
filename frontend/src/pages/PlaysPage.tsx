import { useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  Textarea,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { humanize } from "@/lib/format";
import type { AlertSeverity, Play, PlayAction, PlayInput } from "@/lib/types";
import styles from "./PlaysPage.module.css";

type ActionType = "alert" | "create_task" | "sep_push";

const ACTION_OPTIONS: { value: ActionType; label: string }[] = [
  { value: "alert", label: "Send an alert" },
  { value: "create_task", label: "Create an inbox task" },
  { value: "sep_push", label: "Push to sequence" },
];
const SEVERITY_OPTIONS: { value: AlertSeverity; label: string }[] = [
  { value: "info", label: "Info" },
  { value: "warning", label: "Warning" },
  { value: "critical", label: "Critical" },
];

const EMPTY_FORM = {
  name: "",
  enabled: true,
  signal_kinds: "",
  min_strength: "0.5",
  min_composite: "",
  action_type: "alert" as ActionType,
  message: "",
  body: "",
  severity: "info" as AlertSeverity,
  channel: "in_app",
  sequence: "",
};

const splitCsv = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

export function PlaysPage() {
  const api = useApiClient();
  const toast = useToast();

  const plays = useApi<Play[]>((signal) => api.listPlays(signal), []);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);

  function set<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function buildAction(): PlayAction {
    if (form.action_type === "alert") {
      return {
        type: "alert",
        message: form.message.trim() || undefined,
        body: form.body.trim() || undefined,
        severity: form.severity,
        channel: form.channel.trim() || "in_app",
      };
    }
    if (form.action_type === "sep_push") {
      return { type: "sep_push", sequence: form.sequence.trim() || "default" };
    }
    return { type: "create_task" };
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const body: PlayInput = {
        name: form.name.trim(),
        enabled: form.enabled,
        trigger: {
          signal_kinds: splitCsv(form.signal_kinds),
          min_strength: Number(form.min_strength) || 0,
          min_composite: form.min_composite.trim() ? Number(form.min_composite) : null,
        },
        actions: [buildAction()],
      };
      const created = await api.createPlay(body);
      plays.setData((prev) => [created, ...(prev ?? [])]);
      toast.success("Play created", created.name);
      setModalOpen(false);
      setForm(EMPTY_FORM);
    } catch (err) {
      toast.error(
        "Couldn't create play",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Plays"
        description="Automations that turn signals into action: when a play's trigger matches, its actions fire."
        actions={
          <Button iconLeft={<Icons.PlusIcon />} onClick={() => setModalOpen(true)}>
            New play
          </Button>
        }
      />

      <DataState
        state={plays}
        errorTitle="Couldn't load plays"
        skeleton={
          <div className={styles.grid}>
            {[0, 1, 2].map((i) => (
              <Card key={i} padding="lg">
                <Skeleton width="50%" height={18} />
                <div style={{ height: "var(--space-3)" }} />
                <Skeleton width="100%" height={48} />
              </Card>
            ))}
          </div>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.BoltIcon />}
            title="No plays yet"
            description="Create your first play to automate alerts, tasks, and sequence pushes from signals."
            action={
              <Button iconLeft={<Icons.PlusIcon />} onClick={() => setModalOpen(true)}>
                New play
              </Button>
            }
          />
        }
      >
        {(rows) => (
          <div className={styles.grid}>
            {rows.map((p) => (
              <PlayCard key={p.id} play={p} />
            ))}
          </div>
        )}
      </DataState>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="New play"
        description="Define a trigger and the action it fires."
        footer={
          <>
            <Button variant="secondary" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button form="new-play-form" type="submit" loading={submitting} disabled={!form.name.trim()}>
              Create play
            </Button>
          </>
        }
      >
        <form id="new-play-form" className={styles.form} onSubmit={onSubmit} noValidate>
          <Field label="Play name" required>
            <Input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="Alert on high-intent funding news"
              required
            />
          </Field>

          <label className={styles.toggle}>
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => set("enabled", e.target.checked)}
            />
            <span>Enabled — fires as soon as it's saved</span>
          </label>

          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>Trigger</legend>
            <Field label="Signal kinds" hint="Comma-separated. Leave blank to match any kind.">
              <Input
                value={form.signal_kinds}
                onChange={(e) => set("signal_kinds", e.target.value)}
                placeholder="funding, job_change, intent"
              />
            </Field>
            <div className={styles.grid2}>
              <Field label={`Min strength: ${Number(form.min_strength).toFixed(2)}`} hint="Signal strength, 0–1.">
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={form.min_strength}
                  onChange={(e) => set("min_strength", e.target.value)}
                  className={styles.range}
                  aria-label="Minimum signal strength"
                />
              </Field>
              <Field label="Min fit score" hint="Optional. Composite 0–100.">
                <Input
                  type="number"
                  min={0}
                  max={100}
                  value={form.min_composite}
                  onChange={(e) => set("min_composite", e.target.value)}
                  placeholder="60"
                />
              </Field>
            </div>
          </fieldset>

          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>Action</legend>
            <Field label="When triggered" required>
              <Select
                value={form.action_type}
                onChange={(e) => set("action_type", e.target.value as ActionType)}
                options={ACTION_OPTIONS}
              />
            </Field>

            {form.action_type === "alert" && (
              <>
                <Field label="Alert title" hint="Defaults to the signal's title if blank.">
                  <Input
                    value={form.message}
                    onChange={(e) => set("message", e.target.value)}
                    placeholder="High-intent account is heating up"
                  />
                </Field>
                <Field label="Alert body">
                  <Textarea
                    rows={2}
                    value={form.body}
                    onChange={(e) => set("body", e.target.value)}
                    placeholder="Reach out within 24 hours."
                  />
                </Field>
                <div className={styles.grid2}>
                  <Field label="Severity">
                    <Select
                      value={form.severity}
                      onChange={(e) => set("severity", e.target.value as AlertSeverity)}
                      options={SEVERITY_OPTIONS}
                    />
                  </Field>
                  <Field label="Channel">
                    <Input
                      value={form.channel}
                      onChange={(e) => set("channel", e.target.value)}
                      placeholder="in_app"
                    />
                  </Field>
                </div>
              </>
            )}

            {form.action_type === "sep_push" && (
              <Field label="Sequence" hint="The sequence contacts get pushed into.">
                <Input
                  value={form.sequence}
                  onChange={(e) => set("sequence", e.target.value)}
                  placeholder="default"
                />
              </Field>
            )}

            {form.action_type === "create_task" && (
              <p className={styles.hint}>
                A prioritized inbox task is created for the matching account, using the signal as
                context.
              </p>
            )}
          </fieldset>
        </form>
      </Modal>
    </div>
  );
}

function PlayCard({ play }: { play: Play }) {
  const kinds = play.trigger.signal_kinds ?? [];
  const minStrength = play.trigger.min_strength ?? 0;
  return (
    <Card padding="lg" className={styles.play}>
      <div className={styles.playHead}>
        <h3 className={styles.playName}>{play.name}</h3>
        <Badge tone={play.enabled ? "success" : "neutral"} dot>
          {play.enabled ? "Enabled" : "Paused"}
        </Badge>
      </div>

      <div className={styles.section}>
        <span className={styles.sectionLabel}>When</span>
        <div className={styles.chips}>
          {kinds.length > 0 ? (
            kinds.map((k) => (
              <Badge key={k} tone="neutral">
                {humanize(k)}
              </Badge>
            ))
          ) : (
            <Badge tone="neutral">Any signal</Badge>
          )}
          {minStrength > 0 && <Badge tone="info">≥ {minStrength.toFixed(2)} strength</Badge>}
          {play.trigger.min_composite != null && (
            <Badge tone="info">≥ {play.trigger.min_composite} fit</Badge>
          )}
        </div>
      </div>

      <div className={styles.section}>
        <span className={styles.sectionLabel}>Then</span>
        <div className={styles.chips}>
          {play.actions.length > 0 ? (
            play.actions.map((a, i) => (
              <Badge key={i} tone="accent">
                {humanize(a.type)}
              </Badge>
            ))
          ) : (
            <span className={styles.muted}>No actions</span>
          )}
        </div>
      </div>
    </Card>
  );
}
