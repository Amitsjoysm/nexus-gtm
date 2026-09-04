import { useMemo, useState } from "react";
import { Badge, Button, Card, CardHeader, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { AlertMode, NotificationPreferences } from "@/lib/types";
import styles from "./AlertDelivery.module.css";

/**
 * Where this person's alerts go.
 *
 * `notification_preferences` has had a table, a model and a reader in the digest sweep since
 * migration 0032, and no way to set one. This is that way.
 *
 * **Per user, not per workspace.** A rep muting funding alerts must not mute them for the whole
 * team, which is why the endpoint is rep-level and this lives in Settings rather than an admin
 * console.
 *
 * The grid is categories down, channels across, because the question a person actually has is
 * "where do funding alerts go?" — a flat list of every category/channel pair would be nine rows
 * times five channels and answer it by scrolling.
 */

const MODE_LABEL: Record<AlertMode, string> = {
  immediate: "Send now",
  digest: "Daily digest",
  off: "Never",
};

/** A channel a deployment has compiled in but not configured still appears — it reports "no url
 *  configured" at delivery time rather than vanishing, so hiding it here would be a second, quieter
 *  source of truth about what exists. */
const CHANNEL_LABEL: Record<string, string> = {
  in_app: "In app",
  email: "Email",
  slack: "Slack",
  teams: "Teams",
  webhook: "Webhook",
  telegram: "Telegram",
};

const CATEGORY_LABEL: Record<string, string> = {
  funding: "Funding rounds",
  hiring: "Hiring & leadership",
  champion: "Champion moved",
  intent: "Buying intent",
  technographic: "Tech stack change",
  product: "Product launches",
  news: "Press mentions",
  activity: "Account activity",
  usage: "Product usage",
};

function label(map: Record<string, string>, key: string) {
  return map[key] ?? key.replace(/[._]/g, " ");
}

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

export function AlertDelivery() {
  const api = useApiClient();
  const toast = useToast();
  const prefs = useApi<NotificationPreferences>(
    (signal) => api.notificationPreferences(signal),
    [],
  );
  const [busy, setBusy] = useState<string | null>(null);

  // The browser knows the offset; asking the user for it would be asking them to look it up.
  // Negated because getTimezoneOffset() reports minutes to ADD to local to reach UTC, and the
  // server stores the opposite direction.
  const utcOffsetMin = useMemo(() => -new Date().getTimezoneOffset(), []);

  const chosen = useMemo(() => {
    const map = new Map<string, AlertMode>();
    for (const p of prefs.data?.preferences ?? []) map.set(`${p.category}|${p.channel}`, p.mode);
    return map;
  }, [prefs.data]);

  const quiet = prefs.data?.preferences.find((p) => p.quiet_from_min !== null) ?? null;

  async function choose(category: string, channel: string, mode: AlertMode | null) {
    const key = `${category}|${channel}`;
    setBusy(key);
    try {
      if (mode === null) {
        // Deleting is NOT "off". Off means never send me this; no row means whatever the
        // workspace decides — and this is the only way back to that.
        await api.clearNotificationPreference(category, channel);
      } else {
        await api.setNotificationPreference({
          category,
          channel,
          mode,
          // Carry the existing quiet window so setting a mode does not silently clear it.
          quiet_from_min: quiet?.quiet_from_min ?? null,
          quiet_to_min: quiet?.quiet_to_min ?? null,
          utc_offset_min: utcOffsetMin,
          quiet_hours_allow_critical: quiet?.quiet_hours_allow_critical ?? true,
        });
      }
      prefs.refetch();
    } catch (err) {
      toast.error(
        "Couldn't save that preference",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function saveQuietHours(from: string, to: string, allowCritical: boolean) {
    const f = toMinutes(from);
    const t = toMinutes(to);
    // Half a window is not a window; the server refuses it and so should the form, before a
    // round trip tells the user something they could have been told immediately.
    if ((f === null) !== (t === null)) {
      toast.error("Quiet hours need both a start and an end", "Or clear both to switch them off.");
      return;
    }
    const rows = prefs.data?.preferences ?? [];
    if (rows.length === 0) {
      toast.toast({
        tone: "info",
        title: "Choose a channel first",
        description: "Quiet hours apply to the alerts you have routed somewhere.",
      });
      return;
    }
    setBusy("quiet");
    try {
      // Applied to every routed preference: quiet hours are a property of the PERSON, and a
      // window that held for Slack but not email would be a setting nobody could reason about.
      for (const p of rows) {
        await api.setNotificationPreference({
          category: p.category,
          channel: p.channel,
          mode: p.mode,
          quiet_from_min: f,
          quiet_to_min: t,
          utc_offset_min: utcOffsetMin,
          quiet_hours_allow_critical: allowCritical,
        });
      }
      prefs.refetch();
      toast.success(f === null ? "Quiet hours turned off" : "Quiet hours saved");
    } catch (err) {
      toast.error(
        "Couldn't save quiet hours",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card padding="lg">
      <CardHeader
        title="Alert delivery"
        subtitle="Where your alerts go. These are yours alone — they do not change what your teammates receive."
      />
      <DataState
        state={prefs}
        errorTitle="Couldn't load your alert settings"
        skeleton={<Skeleton width="100%" height={220} />}
      >
        {(data) => (
          <div className={styles.stack}>
            <div className={styles.tableWrap}>
              <table className={styles.grid}>
                <caption className={styles.srOnly}>
                  Alert categories and the channels they are delivered on
                </caption>
                <thead>
                  <tr>
                    <th scope="col" className={styles.rowHead}>
                      Alert
                    </th>
                    {data.channels.map((ch) => (
                      <th key={ch} scope="col">
                        {label(CHANNEL_LABEL, ch)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.categories.map((cat) => (
                    <tr key={cat}>
                      <th scope="row" className={styles.rowHead}>
                        {label(CATEGORY_LABEL, cat)}
                      </th>
                      {data.channels.map((ch) => {
                        const key = `${cat}|${ch}`;
                        const mode = chosen.get(key) ?? null;
                        return (
                          <td key={ch}>
                            <select
                              className={cn(styles.select, mode && styles.selectSet)}
                              value={mode ?? ""}
                              disabled={busy === key}
                              aria-label={`${label(CATEGORY_LABEL, cat)} on ${label(
                                CHANNEL_LABEL,
                                ch,
                              )}`}
                              onChange={(e) =>
                                choose(cat, ch, (e.target.value || null) as AlertMode | null)
                              }
                            >
                              {/* The empty option is "no preference", NOT a default that has been
                                  chosen — the workspace setting still applies. */}
                              <option value="">Workspace default</option>
                              {data.modes.map((m) => (
                                <option key={m} value={m}>
                                  {MODE_LABEL[m] ?? m}
                                </option>
                              ))}
                            </select>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <QuietHours
              from={hhmm(quiet?.quiet_from_min ?? null)}
              to={hhmm(quiet?.quiet_to_min ?? null)}
              allowCritical={quiet?.quiet_hours_allow_critical ?? true}
              busy={busy === "quiet"}
              onSave={saveQuietHours}
            />
          </div>
        )}
      </DataState>
    </Card>
  );
}

function QuietHours({
  from,
  to,
  allowCritical,
  busy,
  onSave,
}: {
  from: string;
  to: string;
  allowCritical: boolean;
  busy: boolean;
  onSave: (from: string, to: string, allowCritical: boolean) => void;
}) {
  const [f, setF] = useState(from);
  const [t, setT] = useState(to);
  const [crit, setCrit] = useState(allowCritical);

  return (
    <section className={styles.quiet} aria-labelledby="quiet-hours">
      <h3 id="quiet-hours" className={styles.quietTitle}>
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
          <input
            type="checkbox"
            checked={crit}
            onChange={(e) => setCrit(e.target.checked)}
          />
          <span>
            Still send critical alerts
            <Badge tone="warning" className={styles.critBadge}>
              recommended
            </Badge>
          </span>
        </label>
        <Button size="sm" loading={busy} onClick={() => onSave(f, t, crit)}>
          Save quiet hours
        </Button>
      </div>
      <p className={styles.quietHint}>
        Someone actively evaluating vendors is a short window. Muting a critical alert overnight can
        cost the deal, which is why this is on by default — but the trade is yours.
      </p>
    </section>
  );
}

export default AlertDelivery;
