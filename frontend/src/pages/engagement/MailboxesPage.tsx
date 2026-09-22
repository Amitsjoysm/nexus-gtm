import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
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
} from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ConnectedMailbox, MailboxProviderState } from "@/lib/types";
import styles from "./MailboxesPage.module.css";

/**
 * My mailboxes: an SDR connects their own Gmail or Microsoft 365 mailbox (spec §9, D1, D2).
 *
 * Campaign email is sent from, and replies are read in, the SDR's own mailbox, so this page belongs
 * to every member rather than to workspace Settings (admin-only). Connecting is OAuth: the browser
 * goes to Google or Microsoft and comes back here with `connected=` or `error=`.
 *
 * The browser's own timezone is sent when connecting, because follow-ups default to the SDR's local
 * working day when a contact's timezone cannot be resolved.
 */

const PROVIDER_LABEL: Record<ConnectedMailbox["provider"], string> = {
  google: "Google (Gmail)",
  microsoft: "Microsoft 365 (Outlook)",
};

const STATUS: Record<ConnectedMailbox["status"], { label: string; tone: BadgeTone }> = {
  connected: { label: "Connected", tone: "success" },
  needs_reauth: { label: "Reconnect needed", tone: "warning" },
  error: { label: "Setup problem", tone: "danger" },
  revoked: { label: "Disconnected", tone: "neutral" },
};

/** Why a connect attempt came back without a mailbox, in words an SDR can act on. */
const CONNECT_ERRORS: Record<string, string> = {
  denied: "You cancelled on the Google or Microsoft screen, so nothing was connected.",
  oauth_failed: "The sign-in did not complete. Try again.",
  bad_state: "That sign-in link expired. Start again from this page.",
  not_configured: "Your administrator has not finished setting up this provider.",
  exchange_failed: "Google or Microsoft refused the sign-in. Try again, or ask your administrator.",
  missing_scopes:
    "Mail access was not granted. Connect again and leave every permission ticked on the consent screen.",
  profile_failed: "The mailbox address could not be read. Try again.",
  owned_by_colleague:
    "A colleague has already connected that mailbox. They need to disconnect it before you can.",
  not_a_member: "You are no longer a member of this workspace.",
};

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function timezoneOptions(current: string) {
  let zones: string[] = [];
  try {
    zones = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf?.("timeZone") ?? [];
  } catch {
    zones = [];
  }
  if (!zones.includes(current)) zones = [current, ...zones];
  return zones.map((z) => ({ value: z, label: z.replace(/_/g, " ") }));
}

export function MailboxesPage() {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const isManager = session?.role === "manager" || session?.role === "admin"
    || session?.role === "owner";
  const [team, setTeam] = useState(false);
  const [connecting, setConnecting] = useState<string | null>(null);

  const providers = useApi<MailboxProviderState[]>((s) => api.connectedMailboxProviders(s), []);
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(team, s), [team]);

  useEffect(() => {
    const connected = params.get("connected");
    const error = params.get("error");
    if (!connected && !error) return;
    if (connected) {
      toast.success("Mailbox connected", "Campaign email will send from it and replies will be read in it.");
    } else if (error) {
      toast.error("Mailbox not connected", CONNECT_ERRORS[error] ?? "Something went wrong. Try again.");
    }
    setParams({}, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connect(provider: "google" | "microsoft") {
    setConnecting(provider);
    try {
      const { authorize_url } = await api.startMailboxConnect(provider, browserTimezone());
      window.location.assign(authorize_url);
    } catch (err) {
      setConnecting(null);
      toast.error("Couldn't start connecting", err instanceof ApiError ? err.detail : "Please try again.");
    }
  }

  return (
    <div>
      <PageHeader
        title="My mailboxes"
        description="Connect the Gmail or Microsoft 365 mailbox you prospect from. Campaign emails send from it, replies are read in it, and nothing else in it is stored."
        actions={
          <>
            <Link to="/do-not-contact" className={styles.link}>Do-not-contact list</Link>
            {isManager && (
              <Button variant="secondary" size="sm" onClick={() => setTeam((t) => !t)}
                aria-pressed={team}>
                {team ? "Show mine" : "Show team"}
              </Button>
            )}
          </>
        }
      />

      <Card padding="lg" className={styles.connect}>
        <h3 className={styles.sectionTitle}>Connect a mailbox</h3>
        <DataState
          state={providers}
          errorTitle="Couldn't check which providers are available"
          skeleton={<Skeleton width="100%" height={44} />}
        >
          {(list) => (
            <div className={styles.providerRow}>
              {list.map((p) => (
                <div key={p.provider} className={styles.provider}>
                  <Button
                    onClick={() => connect(p.provider)}
                    disabled={!p.configured || connecting !== null}
                    loading={connecting === p.provider}
                  >
                    Connect {PROVIDER_LABEL[p.provider]}
                  </Button>
                  {!p.configured && (
                    <span className={styles.hint}>Not set up yet. Ask your administrator.</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </DataState>
      </Card>

      <DataState
        state={mailboxes}
        errorTitle="Couldn't load mailboxes"
        skeleton={<Skeleton width="100%" height={180} />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.MailIcon />}
            title={team ? "Nobody on the team has connected a mailbox" : "No mailbox connected yet"}
            description="Connect one above. Until then campaigns cannot send on your behalf."
          />
        }
      >
        {(rows) => (
          <ul className={styles.list}>
            {rows.map((m) => (
              <MailboxCard key={m.id} mailbox={m} onChanged={mailboxes.refetch}
                onReconnect={() => connect(m.provider)} canDisconnect={m.mine || isManager} />
            ))}
          </ul>
        )}
      </DataState>
    </div>
  );
}

function MailboxCard({
  mailbox, onChanged, onReconnect, canDisconnect,
}: {
  mailbox: ConnectedMailbox;
  onChanged: () => void;
  onReconnect: () => void;
  canDisconnect: boolean;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [timezone, setTimezone] = useState(mailbox.timezone);
  const [signature, setSignature] = useState(mailbox.signature);
  const [confidence, setConfidence] = useState(
    mailbox.reply_confidence === null ? "" : String(mailbox.reply_confidence),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const zones = useMemo(() => timezoneOptions(mailbox.timezone), [mailbox.timezone]);
  const status = STATUS[mailbox.status];
  const dirty = timezone !== mailbox.timezone || signature !== mailbox.signature
    || confidence !== (mailbox.reply_confidence === null ? "" : String(mailbox.reply_confidence));

  async function run(label: string, action: () => Promise<unknown>, success?: [string, string]) {
    setBusy(label);
    try {
      await action();
      if (success) toast.success(success[0], success[1]);
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  function save() {
    const body: Parameters<typeof api.updateConnectedMailbox>[1] = { timezone, signature };
    if (confidence.trim() === "") body.clear_reply_confidence = true;
    else body.reply_confidence = Number(confidence);
    return run("save", () => api.updateConnectedMailbox(mailbox.id, body), ["Saved", "Your mailbox settings are updated."]);
  }

  return (
    <li>
      <Card padding="lg" className={styles.card}>
        <div className={styles.cardHead}>
          <div>
            <p className={styles.email}>{mailbox.email}</p>
            <p className={styles.meta}>{PROVIDER_LABEL[mailbox.provider]}</p>
          </div>
          <Badge tone={status.tone} dot>{status.label}</Badge>
        </div>

        {mailbox.last_error && <p className={styles.error} role="status">{mailbox.last_error}</p>}

        {mailbox.mine && mailbox.status !== "revoked" && (
          <div className={styles.fields}>
            <Field label="Your timezone" hint="Used when a contact's own timezone is unknown.">
              <Select value={timezone} onChange={(e) => setTimezone(e.target.value)} options={zones} />
            </Field>
            <Field
              label="Reply confidence bar"
              hint={`How sure the AI must be before a reply is acted on without you. Leave blank for the workspace default (${mailbox.effective_reply_confidence.toFixed(2)}); allowed ${mailbox.reply_confidence_min.toFixed(2)}–${mailbox.reply_confidence_max.toFixed(2)}.`}
            >
              <Input
                type="number"
                inputMode="decimal"
                step="0.01"
                min={mailbox.reply_confidence_min}
                max={mailbox.reply_confidence_max}
                value={confidence}
                onChange={(e) => setConfidence(e.target.value)}
                placeholder={mailbox.effective_reply_confidence.toFixed(2)}
              />
            </Field>
            <Field label="Signature" hint="Added under every email this mailbox sends. Plain text.">
              <Textarea rows={4} value={signature} onChange={(e) => setSignature(e.target.value)} />
            </Field>
          </div>
        )}

        <div className={styles.actions}>
          {mailbox.mine && mailbox.status !== "revoked" && (
            <Button onClick={save} disabled={!dirty} loading={busy === "save"}>Save</Button>
          )}
          {mailbox.mine && mailbox.status === "connected" && (
            <Button variant="secondary" loading={busy === "check"}
              onClick={() => run("check", () => api.checkConnectedMailbox(mailbox.id),
                ["Checked", "The mailbox signed in."])}>
              Check connection
            </Button>
          )}
          {mailbox.mine && (mailbox.status === "needs_reauth" || mailbox.status === "revoked") && (
            <Button variant="secondary" onClick={onReconnect}>Reconnect</Button>
          )}
          {canDisconnect && mailbox.status !== "revoked" && (
            <Button variant="ghost" onClick={() => setConfirming(true)}>Disconnect</Button>
          )}
        </div>
      </Card>

      <Modal
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Disconnect ${mailbox.email}?`}
        description="Campaign steps from this mailbox pause and replies stop being read. Emails already sent stay in its history."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            <Button variant="danger" loading={busy === "disconnect"}
              onClick={() => run("disconnect", async () => {
                await api.disconnectConnectedMailbox(mailbox.id);
                setConfirming(false);
              }, ["Disconnected", `${mailbox.email} is no longer connected.`])}>
              Disconnect
            </Button>
          </>
        }
      >
        <p className={styles.meta}>You can connect it again at any time.</p>
      </Modal>
    </li>
  );
}

export default MailboxesPage;
