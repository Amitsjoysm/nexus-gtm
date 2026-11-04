import { Fragment, useEffect, useMemo, useState } from "react";
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
  SwitchRow,
  Textarea,
} from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { MailboxHealth } from "@/components/engagement/MailboxHealth";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate, timeAgo } from "@/lib/format";
import type { ConnectedMailbox, MailboxOwnStyle, MailboxProviderState } from "@/lib/types";
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
 *
 * One table row per mailbox (6.2, 2026-10-29): what an SDR or a manager scans for is which mailbox
 * is whose, whether it works, when it was last read and how much it sent today. A row expands in
 * place to the mailbox's own settings (signature, timezone, confidence bar), the writing style its
 * drafts follow, the reply switches and Check connection. Collapsed by default: a team of twelve
 * mailboxes is twelve lines, not twelve forms.
 */

const PROVIDER_LABEL: Record<ConnectedMailbox["provider"], string> = {
  google: "Google (Gmail)",
  microsoft: "Microsoft 365 (Outlook)",
};

/** The table's short form; the connect buttons keep the full names. */
const PROVIDER_SHORT: Record<ConnectedMailbox["provider"], string> = {
  google: "Gmail",
  microsoft: "Outlook",
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

/** What the first email of a sequence asks for, as Settings > Email style words it. */
const FIRST_ASK: Record<string, string> = {
  interest: "asks whether it is worth a look",
  meeting: "asks for a meeting",
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
  const isAdmin = session?.role === "admin" || session?.role === "owner";
  const [team, setTeam] = useState(false);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());

  const providers = useApi<MailboxProviderState[]>((s) => api.connectedMailboxProviders(s), []);
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(team, s), [team]);

  useEffect(() => {
    const connected = params.get("connected");
    const error = params.get("error");
    if (!connected && !error) return;
    if (connected) {
      toast.success(
        "Mailbox connected",
        "Emails you send or save as drafts from a contact now go through it.",
      );
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

  function toggleRow(id: string) {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div>
      <PageHeader
        title="My mailboxes"
        description="Connect the Gmail or Microsoft 365 mailbox you prospect from. Emails you send from here, one at a time or in a campaign, go out from it. Replies are read in it, and nothing else in it is stored."
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

      <div className={styles.connect}>
        <h4 className={styles.connectTitle}>Connect a mailbox</h4>
        <DataState
          state={providers}
          errorTitle="Couldn't check which providers are available"
          skeleton={<Skeleton width={320} height={38} />}
        >
          {(list) => (
            <div className={styles.providerRow}>
              {list.map((p) => (
                <div key={p.provider} className={styles.provider}>
                  <Button
                    variant="secondary"
                    onClick={() => connect(p.provider)}
                    disabled={!p.configured || connecting !== null}
                    loading={connecting === p.provider}
                  >
                    <Icons.PlusIcon /> {PROVIDER_LABEL[p.provider]}
                  </Button>
                  {!p.configured && (
                    <span className={styles.hint}>Not set up yet. Ask your administrator.</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </DataState>
      </div>

      <DataState
        state={mailboxes}
        errorTitle="Couldn't load mailboxes"
        skeleton={<TableSkeleton />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.MailIcon />}
            title={team ? "Nobody on the team has connected a mailbox" : "No mailbox connected yet"}
            description="Connect one above. Until then nothing can be sent from here on your behalf."
          />
        }
      >
        {(rows) => (
          <Card padding="none" className={styles.tableCard}>
            <div className={styles.scroll}>
              <table className={styles.table}>
                <caption className="sr-only">
                  {team ? "The team's mailboxes" : "Your mailboxes"}. Open a row for its settings.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Mailbox</th>
                    <th scope="col">Owner</th>
                    <th scope="col" className={styles.hideNarrow}>Provider</th>
                    <th scope="col">Status</th>
                    <th scope="col" className={styles.hideNarrow}>Last read</th>
                    <th scope="col" className={styles.num}>Sent today</th>
                    <th scope="col" className={styles.actionsHead}>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <MailboxRow
                      key={m.id}
                      mailbox={m}
                      expanded={open.has(m.id)}
                      onToggle={() => toggleRow(m.id)}
                      onChanged={mailboxes.refetch}
                      onReconnect={() => connect(m.provider)}
                      canDisconnect={m.mine || isManager}
                      canEditStyle={isAdmin}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </DataState>
    </div>
  );
}

/** Loading rows shaped like the table, so nothing jumps when the data lands. */
function TableSkeleton() {
  return (
    <Card padding="none" className={styles.tableCard} aria-busy="true">
      <div className={styles.skeletonRows}>
        {[0, 1, 2].map((i) => (
          <div key={i} className={styles.skeletonRow}>
            <Skeleton width="28%" height={14} />
            <Skeleton width="12%" height={14} />
            <Skeleton width="14%" height={20} />
            <Skeleton width="10%" height={14} />
          </div>
        ))}
      </div>
    </Card>
  );
}

function MailboxRow({
  mailbox, expanded, onToggle, onChanged, onReconnect, canDisconnect, canEditStyle,
}: {
  mailbox: ConnectedMailbox;
  expanded: boolean;
  onToggle: () => void;
  onChanged: () => void;
  onReconnect: () => void;
  canDisconnect: boolean;
  canEditStyle: boolean;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [timezone, setTimezone] = useState(mailbox.timezone);
  const [signature, setSignature] = useState(mailbox.signature);
  const [confidence, setConfidence] = useState(
    mailbox.reply_confidence === null ? "" : String(mailbox.reply_confidence),
  );
  const [own, setOwn] = useState<StyleDraft>(() => draftOf(mailbox.own_style));
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmingSend, setConfirmingSend] = useState(false);
  const zones = useMemo(() => timezoneOptions(mailbox.timezone), [mailbox.timezone]);
  const status = STATUS[mailbox.status];
  const dirty = timezone !== mailbox.timezone || signature !== mailbox.signature
    || confidence !== (mailbox.reply_confidence === null ? "" : String(mailbox.reply_confidence))
    || !sameDraft(own, draftOf(mailbox.own_style));
  const panelId = `mailbox-${mailbox.id}`;
  const editable = mailbox.mine && mailbox.status !== "revoked";
  const advised = mailbox.daily_advised ?? 50;
  const over = mailbox.sent_today > advised;
  const paused = mailbox.paused_until && new Date(mailbox.paused_until).getTime() > Date.now()
    ? mailbox.paused_until : null;

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

  // The reply switches apply the moment they are flipped; the server refuses turning one on where
  // the workspace does not allow it, and never refuses turning one off.
  function toggle(key: "auto_actions" | "auto_send_replies", next: boolean) {
    const done: Record<typeof key, [string, string]> = {
      auto_actions: next
        ? ["Turned on", "Clear replies to this mailbox are acted on by themselves."]
        : ["Turned off", "Every reply to this mailbox now waits for you."],
      auto_send_replies: next
        ? ["Turned on", "The AI now answers interested buyers and questions from this mailbox."]
        : ["Turned off", "The AI drafts answers; you send them."],
    };
    return run(key, () => api.updateConnectedMailbox(mailbox.id, { [key]: next }), done[key]);
  }

  function save() {
    const body: Parameters<typeof api.updateConnectedMailbox>[1] = {
      timezone, signature, style: styleOf(own),
    };
    if (confidence.trim() === "") body.clear_reply_confidence = true;
    else body.reply_confidence = Number(confidence);
    return run("save", () => api.updateConnectedMailbox(mailbox.id, body), ["Saved", "Your mailbox settings are updated."]);
  }

  function check() {
    return run("check", () => api.checkConnectedMailbox(mailbox.id), ["Checked", "The mailbox signed in."]);
  }

  return (
    <Fragment>
      <tr className={cn(styles.row, expanded && styles.rowOpen)}>
        <td className={styles.addressCell}>
          <button
            type="button"
            className={styles.disclose}
            aria-expanded={expanded}
            aria-controls={panelId}
            onClick={onToggle}
          >
            <Icons.ChevronRightIcon className={styles.chevron} aria-hidden="true" />
            <span className={styles.addressText}>
              <span className={styles.email}>{mailbox.email}</span>
              {mailbox.display_name && (
                <span className={styles.sub}>Sends as {mailbox.display_name}</span>
              )}
            </span>
          </button>
        </td>
        <td className={styles.owner}>{mailbox.mine ? "You" : mailbox.owner_name || "A colleague"}</td>
        <td className={cn(styles.muted, styles.hideNarrow)}>{PROVIDER_SHORT[mailbox.provider]}</td>
        <td className={styles.statusCell}>
          <Badge tone={status.tone} dot>{status.label}</Badge>
          {paused && (
            <span className={styles.reason}>Paused by the provider until {formatDate(paused)}</span>
          )}
          {mailbox.last_error && <span className={styles.reason} title={mailbox.last_error}>{mailbox.last_error}</span>}
        </td>
        <td className={cn(styles.muted, styles.hideNarrow)}>
          <span title={mailbox.last_synced_at ? new Date(mailbox.last_synced_at).toLocaleString() : undefined}>
            {mailbox.last_synced_at ? timeAgo(mailbox.last_synced_at) : "Never"}
          </span>
        </td>
        <td className={styles.num}>
          <span className={styles.today} title={`${advised} a day is advised, not a cap.`}>
            {over ? <Badge tone="warning" dot>{mailbox.sent_today}</Badge> : mailbox.sent_today}
            <span className={styles.of}> of {advised}</span>
          </span>
        </td>
        {/* The one thing to do to this mailbox now. Disconnect is in its detail: a destructive
            action is one deliberate step away, not beside Check in every row. */}
        <td className={styles.rowActions}>
          {mailbox.mine && (mailbox.status === "needs_reauth" || mailbox.status === "revoked") && (
            <Button size="sm" variant="secondary" onClick={onReconnect}>Reconnect</Button>
          )}
          {mailbox.mine && mailbox.status === "connected" && (
            <Button size="sm" variant="secondary" loading={busy === "check"} onClick={check}>
              Check
            </Button>
          )}
        </td>
      </tr>

      {expanded && (
        <tr className={styles.detailRow}>
          <td colSpan={7} id={panelId}>
            <div className={styles.detail}>
              <section className={styles.detailMain} aria-label={`Settings for ${mailbox.email}`}>
                {mailbox.last_error && <p className={styles.warning} role="status">{mailbox.last_error}</p>}
                {mailbox.volume_warning && (
                  <p className={styles.warning} role="status">{mailbox.volume_warning}</p>
                )}
                <MailboxHealth mailbox={mailbox} />
                {editable ? (
                  <>
                    <Field label="Signature" hint="Added under every email this mailbox sends. Plain text.">
                      <Textarea rows={4} value={signature} onChange={(e) => setSignature(e.target.value)} />
                    </Field>
                    <StyleFields mailbox={mailbox} value={own} onChange={setOwn} />
                    <div className={styles.pair}>
                      <Field label="Your timezone" hint="Used when a contact's own timezone is unknown.">
                        <Select value={timezone} onChange={(e) => setTimezone(e.target.value)} options={zones} />
                      </Field>
                      <Field
                        label="Reply confidence bar"
                        hint={`Blank uses the workspace default (${mailbox.effective_reply_confidence.toFixed(2)}); allowed ${mailbox.reply_confidence_min.toFixed(2)}–${mailbox.reply_confidence_max.toFixed(2)}.`}
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
                    </div>
                    {/* Save sits with the fields it saves; the switches apply the moment they flip. */}
                    <div className={styles.saveRow}>
                      <Button onClick={save} disabled={!dirty} loading={busy === "save"}>Save changes</Button>
                      {mailbox.status === "connected" && (
                        <Button variant="secondary" loading={busy === "check"} onClick={check}>
                          Check connection
                        </Button>
                      )}
                    </div>
                  </>
                ) : (
                  <p className={styles.hint}>
                    {mailbox.mine
                      ? "Reconnect this mailbox to change its settings."
                      : `Only ${mailbox.owner_name || "its owner"} can change this mailbox's signature and switches.`}
                  </p>
                )}
                {canDisconnect && mailbox.status !== "revoked" && (
                  <div className={styles.dangerRow}>
                    <Button variant="ghost" onClick={() => setConfirming(true)}>Disconnect this mailbox</Button>
                  </div>
                )}
              </section>

              <div className={styles.detailSide}>
                <WritingStyle mailbox={mailbox} canEdit={canEditStyle} />
                {editable && (
                  <section className={styles.replies} aria-labelledby={`replies-${mailbox.id}`}>
                    <h5 id={`replies-${mailbox.id}`} className={styles.sideTitle}>When a buyer replies</h5>
                    <SwitchRow
                      label="Act on clear replies by itself"
                      hint={'A confident "not now", "no thanks" or "unsubscribe" snoozes or stops the sequence straight away. Off: the sequence pauses and the reply waits for you.'}
                      status={!mailbox.auto_actions_allowed
                        ? "Not in force: a manager has switched this off for the workspace in Settings, Replies."
                        : undefined}
                      checked={mailbox.auto_actions}
                      disabled={busy !== null || (!mailbox.auto_actions_allowed && !mailbox.auto_actions)}
                      onChange={(next) => toggle("auto_actions", next)}
                    />
                    <SwitchRow
                      label="Let the AI send replies"
                      hint="The AI emails its answer to a buyer who is interested or asked a question, when it is confident and the draft passes every check. Off: it drafts the answer and you send it."
                      status={!mailbox.auto_send_allowed
                        ? (mailbox.auto_send_replies
                          ? "Not in force: a manager has not allowed this in Settings, Replies."
                          : "A manager can allow this in Settings, Replies.")
                        : mailbox.sends_alone
                          ? "On: you get an alert for each reply the AI sends."
                          : undefined}
                      checked={mailbox.auto_send_replies}
                      disabled={busy !== null || (!mailbox.auto_send_allowed && !mailbox.auto_send_replies)}
                      onChange={(next) => (next ? setConfirmingSend(true) : toggle("auto_send_replies", false))}
                    />
                  </section>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}

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
        <p className={styles.hint}>You can connect it again at any time.</p>
      </Modal>

      <Modal
        open={confirmingSend}
        onClose={() => setConfirmingSend(false)}
        title={`Let the AI send replies from ${mailbox.email}?`}
        description="Emails go to real buyers in your name without you reading them first."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmingSend(false)}>Cancel</Button>
            <Button loading={busy === "auto_send_replies"}
              onClick={async () => {
                await toggle("auto_send_replies", true);
                setConfirmingSend(false);
              }}>
              Turn on
            </Button>
          </>
        }
      >
        <ul className={styles.terms}>
          <li>It answers only a buyer who is interested or asked a question, and only when it is confident and its draft passes every check. Anything else waits for you.</li>
          <li>It never offers meeting times, because it cannot see your calendar. It asks which days suit them.</li>
          <li>It answers once per conversation. After that, their replies come to you.</li>
          <li>You get an alert for each one, and can turn this off at any time.</li>
        </ul>
      </Modal>
    </Fragment>
  );
}

/**
 * The tone and shape drafts from this mailbox follow, in force: the mailbox's own fields over the
 * workspace's (2026-11-04). The owner edits theirs beside the signature; an admin edits the
 * workspace defaults in Email defaults.
 */
function WritingStyle({ mailbox, canEdit }: { mailbox: ConnectedMailbox; canEdit: boolean }) {
  const style = mailbox.writing_style;
  const ownSet = Object.values(styleOf(draftOf(mailbox.own_style)))
    .some((v) => (Array.isArray(v) ? v.length > 0 : v !== null));
  const samples = style?.sample_count ?? 0;
  const parts = [
    style?.tone ? `Tone: ${style.tone}.` : "No tone set: drafts follow the house playbook.",
    style?.length_words ? `About ${style.length_words} words.` : null,
    `The first email ${FIRST_ASK[style?.first_ask ?? "interest"] ?? FIRST_ASK.interest}.`,
    style?.ps_line ? "First emails may carry a P.S. you confirm." : null,
    samples ? `Follows ${samples} sample email${samples === 1 ? "" : "s"}.` : null,
  ].filter(Boolean);
  return (
    <section className={styles.style} aria-labelledby={`style-${mailbox.id}`}>
      <h5 id={`style-${mailbox.id}`} className={styles.sideTitle}>Writing style in force</h5>
      <p className={styles.styleText}>{parts.join(" ")}</p>
      <p className={styles.hint}>
        {ownSet ? "This mailbox's own choices, with the workspace's for the rest. " : "The workspace defaults. "}
        {canEdit
          ? <Link to="/settings/email" className={styles.inlineLink}>Edit the workspace defaults</Link>
          : null}
      </p>
    </section>
  );
}

/** The owner's own style, as the form holds it: blank means "use the workspace's". */
interface StyleDraft {
  tone: string;
  length: string;
  firstAsk: "" | "interest" | "meeting";
  ps: "" | "on" | "off";
  samples: [string, string, string];
}

function draftOf(own: ConnectedMailbox["own_style"]): StyleDraft {
  const samples = own?.samples ?? [];
  return {
    tone: own?.tone ?? "",
    length: own?.length_words ? String(own.length_words) : "",
    firstAsk: own?.first_ask ?? "",
    ps: own?.ps_line === true ? "on" : own?.ps_line === false ? "off" : "",
    samples: [samples[0] ?? "", samples[1] ?? "", samples[2] ?? ""],
  };
}

function styleOf(d: StyleDraft): MailboxOwnStyle {
  const length = d.length.trim() ? Number(d.length) : null;
  return {
    tone: d.tone.trim() || null,
    length_words: length !== null && Number.isFinite(length) ? Math.round(length) : null,
    first_ask: d.firstAsk || null,
    ps_line: d.ps === "" ? null : d.ps === "on",
    samples: d.samples.map((x) => x.trim()).filter(Boolean),
  };
}

function sameDraft(a: StyleDraft, b: StyleDraft): boolean {
  return JSON.stringify(styleOf(a)) === JSON.stringify(styleOf(b));
}

/** The mailbox's own writing style. Every field left blank uses the workspace default it names. */
function StyleFields({ mailbox, value, onChange }: {
  mailbox: ConnectedMailbox;
  value: StyleDraft;
  onChange: (next: StyleDraft) => void;
}) {
  const ws = mailbox.workspace_style;
  const set = (patch: Partial<StyleDraft>) => onChange({ ...value, ...patch });
  const wsAsk = FIRST_ASK[ws?.first_ask ?? "interest"] ?? FIRST_ASK.interest;
  const wsSamples = ws?.sample_count ?? 0;
  return (
    <fieldset className={styles.styleFields}>
      <legend className={styles.sideTitle}>This mailbox&apos;s writing style</legend>
      <p className={styles.hint}>Leave a field blank to use the workspace default shown in it.</p>
      <div className={styles.pair}>
        <Field label="Tone">
          <Input value={value.tone} maxLength={200} onChange={(e) => set({ tone: e.target.value })}
            placeholder={ws?.tone || "The house playbook"} />
        </Field>
        <Field label="Length in words" hint="40 to 200.">
          <Input type="number" inputMode="numeric" min={40} max={200} value={value.length}
            onChange={(e) => set({ length: e.target.value })}
            placeholder={ws?.length_words ? String(ws.length_words) : "About 90"} />
        </Field>
      </div>
      <div className={styles.pair}>
        <Field label="The first email asks for">
          <Select value={value.firstAsk}
            onChange={(e) => set({ firstAsk: e.target.value as StyleDraft["firstAsk"] })}
            options={[
              { value: "", label: `Workspace default (${wsAsk})` },
              { value: "interest", label: FIRST_ASK.interest },
              { value: "meeting", label: FIRST_ASK.meeting },
            ]} />
        </Field>
        <Field label="A P.S. you confirm">
          <Select value={value.ps} onChange={(e) => set({ ps: e.target.value as StyleDraft["ps"] })}
            options={[
              { value: "", label: `Workspace default (${ws?.ps_line ? "on" : "off"})` },
              { value: "on", label: "On" },
              { value: "off", label: "Off" },
            ]} />
        </Field>
      </div>
      <Field label="Sample emails" hint={wsSamples
        ? `Up to three emails whose shape and voice drafts follow, never their facts. Empty uses the workspace's ${wsSamples}.`
        : "Up to three emails whose shape and voice drafts follow, never their facts. Plain text."}>
        <div className={styles.samples}>
          {value.samples.map((sample, i) => (
            <Textarea key={i} rows={3} value={sample} aria-label={`Sample email ${i + 1}`}
              placeholder={i === 0 ? "Paste an email you are proud of" : undefined}
              onChange={(e) => {
                const next = [...value.samples] as StyleDraft["samples"];
                next[i] = e.target.value;
                set({ samples: next });
              }} />
          ))}
        </div>
      </Field>
    </fieldset>
  );
}

export default MailboxesPage;
