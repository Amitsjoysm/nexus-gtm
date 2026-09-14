import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Card, CardHeader, Skeleton, useToast } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type {
  AlertChannelConnection,
  AlertChannelConnections,
  NotificationPreferences,
} from "@/lib/types";
import { CATEGORY_LABEL, CHANNEL_LABEL, label } from "./vocabulary";
import styles from "./ChannelRules.module.css";

/**
 * Team channels: which alert types each shared channel receives for everyone. Manager and up.
 *
 * The team half of routing. A member's own route is theirs, and their own "never" and quiet hours
 * apply to it; a rule here is a decision about a SHARED channel, so nobody's personal setting mutes
 * it. It is also the only way an alert on an account nobody owns reaches a channel at all.
 *
 * The category list is read from the server, which derives it from the alert rules, so a type that
 * nothing can emit is never offered as a box to tick.
 */
export function ChannelRules() {
  const api = useApiClient();
  const connections = useApi<AlertChannelConnections>((signal) => api.alertConnections(signal), []);
  const vocabulary = useApi<NotificationPreferences>(
    (signal) => api.notificationPreferences(signal),
    [],
  );

  return (
    <Card padding="lg">
      <CardHeader
        title="Team channels"
        subtitle="Send an alert type to a shared channel for the whole team. Personal settings never mute these."
      />
      <DataState
        state={connections}
        errorTitle="Couldn't load your channels"
        skeleton={<Skeleton width="100%" height={180} />}
      >
        {(data) => (
          <ul className={styles.list}>
            {data.channels.map((channel) => (
              <ChannelRuleRow
                key={channel.kind}
                channel={channel}
                categories={vocabulary.data?.categories ?? null}
                onSaved={() => connections.refetch()}
              />
            ))}
          </ul>
        )}
      </DataState>
    </Card>
  );
}

function ChannelRuleRow({
  channel,
  categories,
  onSaved,
}: {
  channel: AlertChannelConnection;
  /** `null` while the vocabulary is still loading. */
  categories: string[] | null;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [picked, setPicked] = useState<Set<string>>(() => new Set(channel.categories));
  const [saving, setSaving] = useState(false);

  // A refetch after saving hands back the stored set; take it, so the form shows what is saved.
  useEffect(() => setPicked(new Set(channel.categories)), [channel.categories]);

  const dirty = useMemo(() => {
    const saved = new Set(channel.categories);
    return picked.size !== saved.size || [...picked].some((c) => !saved.has(c));
  }, [picked, channel.categories]);

  const name = label(CHANNEL_LABEL, channel.kind);
  const status = channel.connected
    ? { tone: "success" as const, text: "Connected" }
    : channel.needs_reconnect
      ? { tone: "warning" as const, text: "Needs reconnecting" }
      : { tone: "neutral" as const, text: "Not connected yet" };

  function toggle(category: string, on: boolean) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (on) next.add(category);
      else next.delete(category);
      return next;
    });
  }

  async function save() {
    setSaving(true);
    try {
      const res = await api.setAlertChannelRules(channel.kind, [...picked].sort());
      const n = res.categories.length;
      const types = `${n} alert type${n === 1 ? "" : "s"}`;
      toast.success(
        `${name} rules saved`,
        n === 0
          ? "No alert types go there for the team. Members can still route their own."
          : channel.connected
            ? `${types} now post there for the whole team.`
            : `${types} will post there for the whole team once ${name} is connected.`,
      );
      onSaved();
    } catch (err) {
      toast.error(
        `Couldn't save ${name} rules`,
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <li className={styles.row}>
      <fieldset className={styles.fieldset} disabled={saving}>
        <legend className={styles.legend}>
          <span className={styles.channelName}>{name}</span>
          <Badge tone={status.tone} dot>
            {status.text}
          </Badge>
        </legend>
        <div className={styles.body}>
          {!channel.connected && (
            <p className={styles.note}>
              Rules are kept, and start posting as soon as {name} is connected.
            </p>
          )}
          {categories === null ? (
            <Skeleton width="100%" height={48} />
          ) : (
            <div className={styles.categories}>
              {categories.map((category) => (
                <label key={category} className={styles.check}>
                  <input
                    type="checkbox"
                    checked={picked.has(category)}
                    onChange={(e) => toggle(category, e.target.checked)}
                  />
                  <span>{label(CATEGORY_LABEL, category)}</span>
                </label>
              ))}
            </div>
          )}
          <div className={styles.actions}>
            <Button size="sm" onClick={save} loading={saving} disabled={!dirty}>
              Save {name} rules
            </Button>
          </div>
        </div>
      </fieldset>
    </li>
  );
}

export default ChannelRules;
