import { useState } from "react";
import { Badge, Button, Card, CardHeader, Icons, Modal, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import type { AlertChannelConnection, AlertChannelConnections } from "@/lib/types";
import { ConnectChannelForm } from "./ConnectChannelForm";
import { useCanConnectChannels } from "./useCanConnect";
import { CHANNEL_HELP, CHANNEL_LABEL, label } from "./vocabulary";
import styles from "./AlertChannelsCard.module.css";

/**
 * Where this workspace's alerts can be delivered: connect, test, disconnect.
 *
 * Until this existed the channels were deployment environment variables, so a customer could not
 * connect their own Slack at all — and an operator who set one to help a single customer would have
 * routed every workspace's account names and funding signals into it. That is the CRM credential
 * bug in a new place; see `nexus/alerts/connections.py`.
 *
 * **Connected does not mean working, and the two are shown separately.** A saved credential is a
 * stored string; only a real test send advances the status to verified. "Configured and delivering
 * nothing" is indistinguishable from a quiet week, and it is the state this codebase keeps having
 * to diagnose, so the row says which of the two it is.
 */
export function AlertChannelsCard() {
  const api = useApiClient();
  const state = useApi<AlertChannelConnections>((signal) => api.alertConnections(signal), []);
  const canManage = useCanConnectChannels();

  return (
    <Card padding="lg" className={styles.card}>
      <CardHeader
        title="Alert channels"
        subtitle={
          canManage
            ? "Connect the places your team's alerts should arrive. Credentials are encrypted and never shown again."
            : "Where this workspace's alerts can arrive. Only an owner or admin can connect a channel."
        }
      />
      <DataState
        state={state}
        errorTitle="Couldn't load alert channels"
        skeleton={<Skeleton width="100%" height={180} />}
      >
        {(data) => (
          <ul className={styles.list}>
            {data.channels.map((channel) => (
              <ChannelRow
                key={channel.kind}
                channel={channel}
                canManage={canManage}
                onChanged={() => state.refetch()}
              />
            ))}
          </ul>
        )}
      </DataState>
      <p className={styles.footnote}>
        A channel nobody has connected falls back to whatever this deployment was configured with,
        which is usually nothing. In-app alerts always work and need no connection.
      </p>
    </Card>
  );
}

/** Badge tone and wording per state. Four states, not two: connected-but-untested and
 *  connected-but-broken are different problems with different fixes. */
function statusChip(c: AlertChannelConnection): {
  tone: "success" | "warning" | "danger" | "neutral";
  text: string;
} {
  if (c.needs_reconnect) return { tone: "danger", text: "Needs reconnecting" };
  if (!c.connected) return { tone: "neutral", text: "Not connected" };
  if (c.status === "error") return { tone: "danger", text: "Last test failed" };
  if (c.status === "connected" && c.verified_at) return { tone: "success", text: "Verified" };
  return { tone: "warning", text: "Not tested" };
}

function ChannelRow({
  channel,
  canManage,
  onChanged,
}: {
  channel: AlertChannelConnection;
  canManage: boolean;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [testing, setTesting] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);

  const name = label(CHANNEL_LABEL, channel.kind);
  const chip = statusChip(channel);

  async function test() {
    setTesting(true);
    try {
      const next = await api.testAlertChannel(channel.kind);
      onChanged();
      if (next.status === "connected") {
        toast.success(`Test sent to ${name}`, "If it arrived, this channel is ready.");
      } else {
        toast.error(`${name} test failed`, next.last_error || "Nothing was delivered.");
      }
    } catch (err) {
      toast.error(
        `Couldn't test ${name}`,
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setTesting(false);
    }
  }

  async function disconnect() {
    try {
      await api.disconnectAlertChannel(channel.kind);
      setConfirmClear(false);
      onChanged();
      toast.success(`${name} disconnected`, "Alerts routed here stop arriving.");
    } catch (err) {
      toast.error(
        `Couldn't disconnect ${name}`,
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    }
  }

  return (
    <li className={styles.row}>
      <div className={styles.rowHead}>
        <div className={styles.rowText}>
          <span className={styles.name}>{name}</span>
          <span className={styles.desc}>
            {channel.connected && !channel.needs_reconnect
              ? channel.verified_at
                ? `Last verified ${new Date(channel.verified_at).toLocaleString()}.`
                : "Credential saved. Send a test to confirm messages arrive."
              : CHANNEL_HELP[channel.kind]}
          </span>
          {channel.last_error && (
            <span className={styles.error} role="status">
              {channel.last_error}
            </span>
          )}
        </div>
        <Badge tone={chip.tone} dot>
          {chip.text}
        </Badge>
      </div>

      {canManage && (
        <div className={styles.actions}>
          <Button
            size="sm"
            variant={channel.connected && !channel.needs_reconnect ? "ghost" : "secondary"}
            iconLeft={<Icons.PlugIcon />}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {channel.connected && !channel.needs_reconnect
              ? open
                ? "Close"
                : "Replace credential"
              : open
                ? "Close"
                : `Connect ${name}`}
          </Button>
          {channel.connected && (
            <>
              <Button
                size="sm"
                variant="ghost"
                iconLeft={<Icons.ShieldCheckIcon />}
                loading={testing}
                onClick={test}
              >
                Send test
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmClear(true)}>
                Disconnect
              </Button>
            </>
          )}
        </div>
      )}

      {open && canManage && (
        <div className={styles.formWrap}>
          <ConnectChannelForm
            channel={channel}
            canManage={canManage}
            autoFocus
            submitLabel={channel.connected ? "Save credential" : `Connect ${name}`}
            onCancel={() => setOpen(false)}
            onConnected={() => {
              setOpen(false);
              onChanged();
            }}
          />
        </div>
      )}

      <Modal
        open={confirmClear}
        onClose={() => setConfirmClear(false)}
        title={`Disconnect ${name}?`}
        description="The stored credential is deleted. Alerts routed to this channel stop arriving until it is connected again."
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmClear(false)}>
              Keep it connected
            </Button>
            <Button variant="danger" onClick={disconnect}>
              Disconnect {name}
            </Button>
          </>
        }
      >
        <p>You will need the credential again to reconnect.</p>
      </Modal>
    </li>
  );
}

export default AlertChannelsCard;
