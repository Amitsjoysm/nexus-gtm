import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Badge, Button, Card, CardHeader, Field, Icons, Select, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import { ConnectChannelForm } from "@/components/alerts/ConnectChannelForm";
import { useCanConnectChannels } from "@/components/alerts/useCanConnect";
import {
  CATEGORY_BLURB,
  CATEGORY_LABEL,
  CHANNEL_LABEL,
  isConnectable,
  label,
} from "@/components/alerts/vocabulary";
import type {
  AlertChannelConnection,
  AlertChannelConnections,
  AlertMode,
  NotificationPreferences,
} from "@/lib/types";
import styles from "./AlertDelivery.module.css";

/**
 * Where this person's alerts go, set one route at a time.
 *
 * This used to be a grid of every category crossed with every channel: nine rows of six dropdowns,
 * all of them empty, and no way to tell that Slack had never been connected. Somebody could set
 * "funding → Slack" and hear nothing forever, because a routing preference and a working channel
 * are two different facts and the grid showed only the first.
 *
 * So the flow is now the question in order: which alert, where should it go, is that place
 * connected, done. The connect step appears exactly when it is the thing standing in the way, which
 * is the only moment the credential is worth asking for.
 *
 * **Per user, not per workspace.** A rep muting funding alerts must not mute them for the team,
 * which is why the preference endpoint is rep-level. The CHANNEL underneath is a workspace
 * decision, so connecting one is admin-only and a rep is told who to ask instead of being handed a
 * form that fails on submit.
 */

const MODE_LABEL: Record<AlertMode, string> = {
  immediate: "Send it straight away",
  digest: "Include it in my daily digest",
  off: "Never send it",
};

/** Shorter wording for the list of routes, where the category is already the subject. */
const MODE_SHORT: Record<AlertMode, string> = {
  immediate: "Straight away",
  digest: "Daily digest",
  off: "Never",
};

export function AlertDelivery() {
  const api = useApiClient();
  const prefs = useApi<NotificationPreferences>(
    (signal) => api.notificationPreferences(signal),
    [],
  );
  // Read separately, and a failure here is NOT allowed to block routing. The connection state
  // improves the flow; it is not what the flow is for.
  const connections = useApi<AlertChannelConnections>((signal) => api.alertConnections(signal), []);

  return (
    <Card padding="lg">
      <CardHeader
        title="Alert delivery"
        subtitle="Pick an alert, pick where it should reach you. These are yours alone and do not change what your teammates receive."
      />
      <DataState
        state={prefs}
        errorTitle="Couldn't load your alert settings"
        skeleton={<Skeleton width="100%" height={220} />}
      >
        {(data) => (
          <div className={styles.stack}>
            <GuidedSetup
              data={data}
              connections={connections.data?.channels ?? []}
              onConnectionsChanged={() => connections.refetch()}
              onSaved={() => prefs.refetch()}
            />
            <RoutesList data={data} onChanged={() => prefs.refetch()} />
            <QuietHoursSection data={data} onSaved={() => prefs.refetch()} />
          </div>
        )}
      </DataState>
    </Card>
  );
}

/* ---- the guided flow ------------------------------------------------------------------------ */

function GuidedSetup({
  data,
  connections,
  onConnectionsChanged,
  onSaved,
}: {
  data: NotificationPreferences;
  connections: AlertChannelConnection[];
  onConnectionsChanged: () => void;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const canConnect = useCanConnectChannels();

  const [category, setCategory] = useState("");
  const [channel, setChannel] = useState("");
  const [mode, setMode] = useState<AlertMode>("immediate");
  const [saving, setSaving] = useState(false);
  /** The route just saved. Holding it is what lets the panel say "these are ready" about a
   *  specific thing rather than flashing a toast that is gone before it is read. */
  const [done, setDone] = useState<{ category: string; channel: string; mode: AlertMode } | null>(
    null,
  );

  const utcOffsetMin = useMemo(() => -new Date().getTimezoneOffset(), []);
  const quiet = data.preferences.find((p) => p.quiet_from_min !== null) ?? null;

  const conn = connections.find((c) => c.kind === channel) ?? null;
  // Only a channel the server says is connectable AND reports as unconnected blocks the route. A
  // connections read that failed leaves `conn` null, which lets the route through — the same bias
  // as everywhere else here: a lookup we could not do must not delete somebody's ability to act.
  const needsConnection = isConnectable(channel) && conn !== null && !conn.connected;

  const existing = data.preferences.find(
    (p) => p.category === category && p.channel === channel,
  );

  function reset() {
    setCategory("");
    setChannel("");
    setMode("immediate");
    setDone(null);
  }

  async function save() {
    setSaving(true);
    try {
      await api.setNotificationPreference({
        category,
        channel,
        mode,
        // Carry the existing window through, or saving a route silently clears quiet hours.
        quiet_from_min: quiet?.quiet_from_min ?? null,
        quiet_to_min: quiet?.quiet_to_min ?? null,
        utc_offset_min: utcOffsetMin,
        quiet_hours_allow_critical: quiet?.quiet_hours_allow_critical ?? true,
      });
      setDone({ category, channel, mode });
      onSaved();
    } catch (err) {
      toast.error(
        "Couldn't save that route",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  if (done) {
    return (
      <section className={styles.ready} aria-live="polite">
        <span className={styles.readyIcon} aria-hidden="true">
          <Icons.CheckIcon />
        </span>
        <div className={styles.readyText}>
          <h3 className={styles.readyTitle}>Alerts are ready</h3>
          <p className={styles.readyBody}>
            {label(CATEGORY_LABEL, done.category)} will reach you on{" "}
            {label(CHANNEL_LABEL, done.channel)}
            {done.mode === "digest" ? " in your daily digest" : ""}
            {done.mode === "off" ? ", and nothing will be sent" : ""}. The next matching signal is
            the first one you will get.
          </p>
        </div>
        <Button size="sm" variant="secondary" iconLeft={<Icons.PlusIcon />} onClick={reset}>
          Route another alert
        </Button>
      </section>
    );
  }

  return (
    <section className={styles.guide} aria-labelledby="alert-setup">
      <h3 id="alert-setup" className={styles.sectionTitle}>
        Set up an alert
      </h3>
      <ol className={styles.steps}>
        <Step n={1} title="Which alert?">
          <Field label="Alert type" hideLabel>
            <Select
              value={category}
              placeholder="Choose an alert type"
              onChange={(e) => setCategory(e.target.value)}
              options={data.categories.map((c) => ({
                value: c,
                label: label(CATEGORY_LABEL, c),
              }))}
            />
          </Field>
          {category && <p className={styles.blurb}>{CATEGORY_BLURB[category] ?? ""}</p>}
        </Step>

        {category && (
          <Step n={2} title="Where should it reach you?">
            <Field label="Delivery channel" hideLabel>
              <Select
                value={channel}
                placeholder="Choose a channel"
                onChange={(e) => setChannel(e.target.value)}
                options={data.channels.map((ch) => ({
                  value: ch,
                  label: channelOptionLabel(ch, connections),
                }))}
              />
            </Field>
            {channel === "webhook" && (
              <p className={styles.blurb}>
                The webhook endpoint is set for the whole deployment by an operator, not per
                workspace.
              </p>
            )}
            {channel === "in_app" && (
              <p className={styles.blurb}>
                Always available. Alerts appear on the Alerts page and in the top bar, with nothing
                to connect.
              </p>
            )}
          </Step>
        )}

        {category && channel && needsConnection && conn && (
          <Step n={3} title={`Connect ${label(CHANNEL_LABEL, channel)} first`}>
            <p className={styles.blurb}>
              {conn.needs_reconnect
                ? "This workspace has a saved credential that no longer works, so nothing is being delivered. Replacing it fixes every route pointing here."
                : "Nobody has connected this channel yet, so an alert routed to it would go nowhere."}
            </p>
            <ConnectChannelForm
              channel={conn}
              canManage={canConnect}
              onConnected={onConnectionsChanged}
            />
          </Step>
        )}

        {category && channel && !needsConnection && (
          <Step n={3} title="How should it arrive?">
            <Field label="Delivery timing" hideLabel>
              <Select
                value={mode}
                onChange={(e) => setMode(e.target.value as AlertMode)}
                options={data.modes
                  // "Never" is a real preference and it lives on the routes below. Offering it
                  // while somebody is adding a route is offering to undo what they came to do.
                  .filter((m) => m !== "off")
                  .map((m) => ({ value: m, label: MODE_LABEL[m] ?? m }))}
              />
            </Field>
            {existing && (
              <p className={styles.blurb}>
                You already route {label(CATEGORY_LABEL, category).toLowerCase()} here
                {existing.mode === mode
                  ? "."
                  : ` as "${MODE_SHORT[existing.mode]}". Saving changes it.`}
              </p>
            )}
            <div className={styles.saveRow}>
              <Button loading={saving} onClick={save}>
                Save this route
              </Button>
              <Button variant="ghost" onClick={reset} disabled={saving}>
                Start over
              </Button>
            </div>
          </Step>
        )}
      </ol>
    </section>
  );
}

/** Channels carry their connection state into the dropdown, so the answer to "will this work?"
 *  arrives before the choice rather than after it. */
function channelOptionLabel(channel: string, connections: AlertChannelConnection[]): string {
  const name = label(CHANNEL_LABEL, channel);
  if (!isConnectable(channel)) return name;
  const conn = connections.find((c) => c.kind === channel);
  if (!conn) return name;
  if (conn.needs_reconnect) return `${name} — needs reconnecting`;
  if (!conn.connected) return `${name} — not connected yet`;
  return `${name} — connected`;
}

/**
 * One numbered step. The number is presentational (`aria-hidden`) because the `<ol>` already
 * announces the position, and hearing "3, three, Connect Slack first" is worse than hearing it once.
 *
 * A step is rendered at full opacity from the first frame and animates only its entrance, so the
 * content is never gated on a transition firing. `useReducedMotion` drops the movement and keeps
 * the step.
 */
function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  const reduced = useReducedMotion();
  return (
    <motion.li
      className={styles.step}
      initial={reduced ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
    >
      <span className={styles.stepNumber} aria-hidden="true">
        {n}
      </span>
      <div className={styles.stepBody}>
        <h4 className={styles.stepTitle}>{title}</h4>
        {children}
      </div>
    </motion.li>
  );
}

/* ---- what is already routed ------------------------------------------------------------------ */

function RoutesList({
  data,
  onChanged,
}: {
  data: NotificationPreferences;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);

  const utcOffsetMin = useMemo(() => -new Date().getTimezoneOffset(), []);
  const quiet = data.preferences.find((p) => p.quiet_from_min !== null) ?? null;

  const rows = useMemo(
    () =>
      [...data.preferences].sort(
        (a, b) =>
          label(CATEGORY_LABEL, a.category).localeCompare(label(CATEGORY_LABEL, b.category)) ||
          a.channel.localeCompare(b.channel),
      ),
    [data.preferences],
  );

  async function change(category: string, channel: string, next: AlertMode | null) {
    const key = `${category}|${channel}`;
    setBusy(key);
    try {
      if (next === null) {
        // Removing a route is NOT setting it to "never". Never means never send me this; no route
        // means whatever the workspace decides, and this is the only way back to that.
        await api.clearNotificationPreference(category, channel);
      } else {
        await api.setNotificationPreference({
          category,
          channel,
          mode: next,
          quiet_from_min: quiet?.quiet_from_min ?? null,
          quiet_to_min: quiet?.quiet_to_min ?? null,
          utc_offset_min: utcOffsetMin,
          quiet_hours_allow_critical: quiet?.quiet_hours_allow_critical ?? true,
        });
      }
      onChanged();
    } catch (err) {
      toast.error(
        "Couldn't update that route",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className={styles.routes} aria-labelledby="alert-routes">
      <h3 id="alert-routes" className={styles.sectionTitle}>
        Your routes
      </h3>
      {rows.length === 0 ? (
        <p className={styles.empty}>
          You have not routed anything yet, so every alert follows the workspace default. Set one
          above and it will appear here.
        </p>
      ) : (
        <>
        <p className={styles.routesHint}>
          Setting a route back to <strong>Workspace default</strong> removes it, which is different
          from <strong>Never</strong>: never means never send it, the default means whatever your
          workspace decides.
        </p>
        <ul className={styles.routeList}>
          {rows.map((p) => {
            const key = `${p.category}|${p.channel}`;
            return (
              <li key={key} className={styles.route}>
                <div className={styles.routeText}>
                  <span className={styles.routeCategory}>
                    {label(CATEGORY_LABEL, p.category)}
                  </span>
                  <span className={styles.routeChannel}>
                    to {label(CHANNEL_LABEL, p.channel)}
                  </span>
                </div>
                {p.mode === "off" && (
                  <Badge tone="neutral" dot>
                    Muted
                  </Badge>
                )}
                {/* One control, not a dropdown beside a delete button, because "Workspace default"
                    and "remove this route" are the SAME act and offering both invites the reading
                    that they differ. Choosing it deletes the row, which is not the same as "Never":
                    never means never send me this, no row means whatever the workspace decides, and
                    this option is the only way back to the second. */}
                <Select
                  className={styles.routeMode}
                  value={p.mode}
                  disabled={busy === key}
                  aria-label={`Delivery for ${label(CATEGORY_LABEL, p.category)} on ${label(
                    CHANNEL_LABEL,
                    p.channel,
                  )}`}
                  onChange={(e) =>
                    change(p.category, p.channel, (e.target.value || null) as AlertMode | null)
                  }
                  options={[
                    ...data.modes.map((m) => ({ value: m, label: MODE_SHORT[m] ?? m })),
                    { value: "", label: "Workspace default" },
                  ]}
                />
              </li>
            );
          })}
        </ul>
        </>
      )}
    </section>
  );
}

/* ---- quiet hours ----------------------------------------------------------------------------- */

/** "22:00" from 1320 minutes past local midnight. */
function hhmm(min: number | null): string {
  if (min === null || min === undefined) return "";
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function toMinutes(value: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  if (!m) return null;
  const mins = Number(m[1]) * 60 + Number(m[2]);
  return mins >= 0 && mins <= 1439 ? mins : null;
}

function QuietHoursSection({
  data,
  onSaved,
}: {
  data: NotificationPreferences;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const quiet = data.preferences.find((p) => p.quiet_from_min !== null) ?? null;
  const utcOffsetMin = useMemo(() => -new Date().getTimezoneOffset(), []);

  const [f, setF] = useState(hhmm(quiet?.quiet_from_min ?? null));
  const [t, setT] = useState(hhmm(quiet?.quiet_to_min ?? null));
  const [crit, setCrit] = useState(quiet?.quiet_hours_allow_critical ?? true);
  const [busy, setBusy] = useState(false);

  async function save() {
    const from = toMinutes(f);
    const to = toMinutes(t);
    // Half a window is not a window. The server refuses it, and so should the form, before a round
    // trip tells the user something they could have been told immediately.
    if ((from === null) !== (to === null)) {
      toast.error("Quiet hours need both a start and an end", "Or clear both to switch them off.");
      return;
    }
    if (data.preferences.length === 0) {
      toast.toast({
        tone: "info",
        title: "Route an alert first",
        description: "Quiet hours apply to the alerts you have routed somewhere.",
      });
      return;
    }
    setBusy(true);
    try {
      // Applied to every route: quiet hours are a property of the PERSON, and a window that held
      // for Slack but not email would be a setting nobody could reason about.
      for (const p of data.preferences) {
        await api.setNotificationPreference({
          category: p.category,
          channel: p.channel,
          mode: p.mode,
          quiet_from_min: from,
          quiet_to_min: to,
          utc_offset_min: utcOffsetMin,
          quiet_hours_allow_critical: crit,
        });
      }
      onSaved();
      toast.success(from === null ? "Quiet hours turned off" : "Quiet hours saved");
    } catch (err) {
      toast.error(
        "Couldn't save quiet hours",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={styles.quiet} aria-labelledby="quiet-hours">
      <h3 id="quiet-hours" className={styles.sectionTitle}>
        Quiet hours
      </h3>
      <p className={styles.quietHint}>
        Alerts raised in this window wait until it ends. Times are in your own timezone, and a
        window that crosses midnight works as you would expect.
      </p>
      <div className={styles.quietRow}>
        <label className={styles.field}>
          <span className={styles.fieldLabel}>From</span>
          <input
            type="time"
            className={styles.time}
            value={f}
            onChange={(e) => setF(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.fieldLabel}>Until</span>
          <input
            type="time"
            className={styles.time}
            value={t}
            onChange={(e) => setT(e.target.value)}
          />
        </label>
        <label className={styles.check}>
          <input type="checkbox" checked={crit} onChange={(e) => setCrit(e.target.checked)} />
          <span>
            Still send critical alerts
            <Badge tone="warning" className={styles.critBadge}>
              recommended
            </Badge>
          </span>
        </label>
        <Button size="sm" loading={busy} onClick={save}>
          Save quiet hours
        </Button>
      </div>
      <p className={styles.quietHint}>
        Somebody actively comparing vendors is a short window, so muting a critical alert overnight
        can cost the deal. That is why this stays on by default, but the trade is yours.
      </p>
    </section>
  );
}

export default AlertDelivery;
