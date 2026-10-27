import { useEffect, useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  Field,
  Icons,
  Input,
  Select,
  Skeleton,
  Textarea,
  useToast,
} from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type {
  CallTaskCampaignScript,
  DispositionCategory,
  CallBrief,
  CallBriefSignal,
  CallScript,
  CallTask,
  TelephonyStatus,
} from "@/lib/types";
import { formatNumber, humanize, signalWhen, signalWhenTitle } from "@/lib/format";
import { emailStatusMeta, strengthMeta } from "@/lib/display";
import styles from "./CallConsole.module.css";

/** What logging a disposition does besides recording it, said before the rep presses Log (D11). */
const ACTION_HINT: Record<string, string> = {
  do_not_contact: "Adds them to do-not-contact: no more emails or calls.",
  phone_bad: "Marks this number bad. It stays on the contact.",
  email_bad: "Removes this email address and remembers it as bad.",
  dated_task: "Keeps this call open, due when you say.",
  flag_account: "Flags the account. Nothing is deleted.",
  find_right_contact: "Adds an Inbox task to find the right contact.",
};

function telHref(phone: string | null): string | null {
  if (!phone) return null;
  const digits = phone.replace(/[^\d+]/g, "");
  return digits ? `tel:${digits}` : null;
}

/** Where the rep's own callback number is remembered — the phone a live bridge rings first. */
const CALLBACK_KEY = "nexus.callback_number";
/** Mirrors the server's E.164 check so a bad number is caught before a request is spent. */
const E164 = /^\+[1-9]\d{7,14}$/;

function normalizeNumber(value: string): string {
  return value.replace(/[\s().\-/]/g, "");
}

export interface CallConsoleProps {
  task: CallTask;
  /** Auto-generate the AI script as soon as the task is shown (great for the account modal). */
  autoGenerate?: boolean;
  /** Called after a disposition is logged (close the modal / refresh the queue). */
  onLogged?: (disposition: string) => void;
}

/**
 * The full single-call workspace: click-to-dial, the AI talk track, and one-tap disposition
 * logging. Shared by the Calls power-list and the per-contact "Call" modal on an account, so the
 * SDR gets the exact same calling experience wherever they start the call.
 */
export function CallConsole({ task, autoGenerate, onLogged }: CallConsoleProps) {
  const api = useApiClient();
  const toast = useToast();
  const [script, setScript] = useState<CallScript | null>(null);
  const [scriptBusy, setScriptBusy] = useState(false);
  const [brief, setBrief] = useState<CallBrief | null>(null);
  const [briefBusy, setBriefBusy] = useState(false);
  const [briefError, setBriefError] = useState(false);
  const [notes, setNotes] = useState("");
  const [nextStep, setNextStep] = useState("");
  const [logging, setLogging] = useState<string | null>(null);
  // The team's Category -> Disposition list, from the server so both channels read one vocabulary.
  const [catalog, setCatalog] = useState<DispositionCategory[]>([]);
  const [category, setCategory] = useState("");
  const [dispositionCode, setDispositionCode] = useState("");
  const [followUpAt, setFollowUpAt] = useState("");
  const inCategory = catalog.find((c) => c.key === category)?.dispositions ?? [];
  const chosen = inCategory.find((d) => d.code === dispositionCode) ?? null;
  // The campaign's own script for this call, when it has one (404 otherwise: the AI one is used).
  const [ownScript, setOwnScript] = useState<CallTaskCampaignScript | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    setOwnScript(null);
    api.callTaskCampaignScript(task.id, ctrl.signal).then(setOwnScript).catch(() => {});
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id]);
  // Telephony is manual (click-to-dial) until the server says otherwise, so the default
  // workflow renders immediately instead of waiting on a status round-trip.
  const [telephony, setTelephony] = useState<TelephonyStatus | null>(null);
  const [callbackNumber, setCallbackNumber] = useState(
    () => localStorage.getItem(CALLBACK_KEY) ?? "",
  );
  const [callbackError, setCallbackError] = useState<string | null>(null);
  const [dialing, setDialing] = useState(false);
  // Set once a live call is placed: it links the outcome to the provider's own record of the
  // call, which is what pulls the real duration, recording, and transcript onto the activity.
  const [liveCallId, setLiveCallId] = useState<string | null>(null);

  const isLive = telephony?.mode === "live";

  /** Load today's script, generating one only if there is none. `refresh` is the Regenerate
   *  button — the only path that always writes a new script (and costs a completion). */
  async function generate(refresh = false) {
    setScriptBusy(true);
    try {
      setScript(await api.generateCallScript(task.id, { refresh }));
    } catch (err) {
      toast.error("Couldn't generate script", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setScriptBusy(false);
    }
  }

  useEffect(() => {
    const ctrl = new AbortController();
    api.callDispositions("call", ctrl.signal).then(setCatalog).catch(() => setCatalog([]));
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** A category with one disposition (Gatekeeper, Other) needs no second choice. */
  function pickCategory(key: string) {
    setCategory(key);
    const only = catalog.find((c) => c.key === key)?.dispositions ?? [];
    setDispositionCode(only.length === 1 ? only[0].code : "");
    setFollowUpAt("");
  }

  // Reset + (optionally) pre-generate whenever the call changes.
  useEffect(() => {
    setScript(null);
    setNotes("");
    setNextStep("");
    setCategory("");
    setDispositionCode("");
    setFollowUpAt("");
    setLiveCallId(null);
    if (autoGenerate) void generate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id]);

  // Can this workspace place live calls? A failure here is not worth surfacing: the console
  // simply stays on click-to-dial, which works without any telephony account.
  useEffect(() => {
    let active = true;
    const ctrl = new AbortController();
    api
      .telephonyStatus(ctrl.signal)
      .then((s) => active && setTelephony(s))
      .catch(() => undefined);
    return () => {
      active = false;
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function dialLive() {
    const number = normalizeNumber(callbackNumber);
    if (!E164.test(number)) {
      setCallbackError("Enter your number in international format, like +15551234567.");
      return;
    }
    setCallbackError(null);
    localStorage.setItem(CALLBACK_KEY, number);
    setDialing(true);
    try {
      // A fresh key per attempt: a double-click replays this response rather than placing a
      // second billable call, while a deliberate retry after hanging up still dials.
      const result = await api.dialCallTask(task.id, number, crypto.randomUUID());
      setLiveCallId(result.provider_call_id);
      toast.success("Calling you now", `Answer your phone to connect to ${task.contact_name ?? "the contact"}.`);
    } catch (err) {
      toast.error("Couldn't place the call", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setDialing(false);
    }
  }

  // Always pull the pre-call research dossier as soon as a call is opened — the SDR should be
  // looking at the person + company + signals (with sources) before they dial.
  useEffect(() => {
    let active = true;
    const ctrl = new AbortController();
    setBrief(null);
    setBriefError(false);
    setBriefBusy(true);
    api
      .callBrief(task.id, ctrl.signal)
      .then((b) => active && setBrief(b))
      .catch(() => active && setBriefError(true))
      .finally(() => active && setBriefBusy(false));
    return () => {
      active = false;
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id]);

  async function logDisposition(disposition: string) {
    setLogging(disposition);
    try {
      await api.logCallDisposition(task.id, {
        disposition,
        notes: notes.trim() || undefined,
        next_step: nextStep.trim() || undefined,
        provider_call_id: liveCallId,
        follow_up_at: followUpAt ? new Date(followUpAt).toISOString() : undefined,
      });
      toast.success("Logged", `${chosen?.label ?? disposition} — ${task.contact_name ?? "contact"}.`);
      onLogged?.(disposition);
    } catch (err) {
      toast.error("Couldn't log call", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setLogging(null);
    }
  }

  const dial = telHref(task.phone);

  return (
    <div className={styles.call}>
      <header className={styles.callHead}>
        <div>
          <h3 className={styles.callName}>{task.contact_name ?? "Unknown contact"}</h3>
          <p className={styles.callMeta}>
            {task.title ? `${task.title} · ` : ""}{task.account_name}
          </p>
        </div>
        {!dial ? (
          <Badge tone="warning" dot>No number</Badge>
        ) : isLive ? (
          <Button onClick={dialLive} loading={dialing} disabled={dialing}>
            <Icons.PhoneIcon /> Start call
          </Button>
        ) : (
          <a className={styles.dialBtn} href={dial}>
            <Icons.PhoneIcon /> Call {task.phone}
          </a>
        )}
      </header>

      {dial && isLive && (
        <section className={styles.bridge}>
          <Field
            label="Your phone"
            hint={`We ring this number first, then connect you to ${task.contact_name ?? "the contact"} at ${task.phone}.`}
            error={callbackError ?? undefined}
          >
            <Input
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              value={callbackNumber}
              onChange={(e) => {
                setCallbackNumber(e.target.value);
                if (callbackError) setCallbackError(null);
              }}
              placeholder="+15551234567"
            />
          </Field>
          <p className={styles.bridgeMeta} role="status">
            {liveCallId ? (
              <>
                <Badge tone="info" dot>In progress</Badge>
                {telephony?.record_calls
                  ? " Recording and transcript attach to the outcome you log below."
                  : " Log the outcome below when you hang up."}
              </>
            ) : (
              <>
                Calls show as {telephony?.from_number || "your workspace number"} to the contact.
                {telephony?.source === "platform" && " Each minute uses credits."}{" "}
                <a className={styles.link} href={dial}>
                  Dial from this device instead
                </a>
              </>
            )}
          </p>
        </section>
      )}

      {task.reason && <p className={styles.reason}>{task.reason}</p>}

      <BriefPanel brief={brief} busy={briefBusy} error={briefError} />

      {/* The team's own script, read out as written (D10); the brief above is its side panel. */}
      {ownScript && (
        <div className={styles.scriptBlock}>
          <div className={styles.scriptHead}>
            <h4>Your script</h4>
            <span className={styles.scriptWhen}>{ownScript.campaign_name}</span>
          </div>
          <pre className={styles.ownScript}>{ownScript.text}</pre>
        </div>
      )}

      <div className={styles.scriptBlock}>
        <div className={styles.scriptHead}>
          <h4>{ownScript ? "AI talk track (for reference)" : "AI call script"}</h4>
          {script?.generated_at && (
            <span className={styles.scriptWhen}>
              Written{" "}
              {new Date(script.generated_at).toLocaleTimeString([], {
                hour: "numeric",
                minute: "2-digit",
              })}{" "}
              today
            </span>
          )}
          <Button
            size="sm"
            variant="secondary"
            loading={scriptBusy}
            onClick={() => generate(Boolean(script))}
          >
            {script ? "Regenerate" : "Generate script"}
          </Button>
        </div>
        {scriptBusy && !script && <Skeleton width="100%" height={140} />}
        {script && (
          <div className={styles.script}>
            <p><strong>Opener.</strong> {script.opener}</p>
            <p><strong>Hook.</strong> {script.hook}</p>
            <p><strong>Value.</strong> {script.value_prop}</p>
            {script.discovery_questions.length > 0 && (
              <div>
                <strong>Discovery</strong>
                <ul>{script.discovery_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
              </div>
            )}
            {script.objections.length > 0 && (
              <div>
                <strong>Objections</strong>
                <ul>
                  {script.objections.map((o, i) => (
                    <li key={i}><em>{o.objection}</em> → {o.response}</li>
                  ))}
                </ul>
              </div>
            )}
            <p><strong>Ask.</strong> {script.cta}</p>
            <p className={styles.vm}><strong>Voicemail.</strong> {script.voicemail}</p>
          </div>
        )}
      </div>

      <div className={styles.logBlock}>
        <h4>Log the outcome</h4>
        <Textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Call notes…"
          rows={2}
          aria-label="Call notes"
        />
        <Input
          value={nextStep}
          onChange={(e) => setNextStep(e.target.value)}
          placeholder="Next step (e.g. demo Tue 2pm)"
          aria-label="Next step"
        />
        <div className={styles.dispoPick}>
          <Field label="Category">
            <Select
              value={category}
              onChange={(e) => pickCategory(e.target.value)}
              placeholder="Choose a category"
              options={catalog.map((c) => ({ value: c.key, label: c.label }))}
              disabled={!catalog.length || !!logging}
            />
          </Field>
          <Field label="Disposition">
            <Select
              value={dispositionCode}
              onChange={(e) => {
                setDispositionCode(e.target.value);
                setFollowUpAt("");
              }}
              placeholder={category ? "Choose an outcome" : "Choose a category first"}
              options={inCategory.map((d) => ({ value: d.code, label: d.label }))}
              disabled={!category || !!logging}
            />
          </Field>
          {chosen?.needs_date && (
            <Field label="Follow up on" hint="Optional. The call stays in your queue either way.">
              <Input
                type="datetime-local"
                value={followUpAt}
                onChange={(e) => setFollowUpAt(e.target.value)}
              />
            </Field>
          )}
        </div>
        {chosen && ACTION_HINT[chosen.action] && (
          <p className={styles.dispoHint}>{ACTION_HINT[chosen.action]}</p>
        )}
        <Button
          variant="primary"
          loading={!!logging}
          disabled={!chosen || !!logging}
          onClick={() => chosen && logDisposition(chosen.code)}
        >
          Log outcome
        </Button>
      </div>
    </div>
  );
}

/** Deliverability verdict → badge tone for the contact's email. */
function emailTone(status: string | null): "success" | "warning" | "danger" | "neutral" | "info" {
  switch (status) {
    case "valid":
      return "success";
    case "catch_all":
      return "info";
    case "risky":
      return "warning";
    case "invalid":
    case "bad_number":
      return "danger";
    default:
      return "neutral";
  }
}

/** A labelled subsection of the brief, with optional source attribution on the right. */
function BriefSub({
  title,
  source,
  children,
}: {
  title: string;
  source?: string | null;
  children: ReactNode;
}) {
  return (
    <section className={styles.briefSub}>
      <div className={styles.briefSubHead}>
        <h5 className={styles.briefSubTitle}>{title}</h5>
        {source && <span className={styles.briefSource}>Source: {source}</span>}
      </div>
      {children}
    </section>
  );
}

/** One label/value row inside a brief subsection. */
function KV({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.kv}>
      <dt className={styles.kvLabel}>{label}</dt>
      <dd className={styles.kvValue}>{children}</dd>
    </div>
  );
}

function SignalRow({ sig }: { sig: CallBriefSignal }) {
  const meta = strengthMeta(sig.strength);
  return (
    <li className={styles.signal}>
      <Badge tone={meta.tone} dot>
        {meta.label}
      </Badge>
      <div className={styles.signalBody}>
        <div className={styles.signalTitle}>
          {sig.title}
          {sig.is_personal && <span className={styles.personTag}>About them</span>}
        </div>
        {sig.body && <p className={styles.signalText}>{sig.body}</p>}
        <div className={styles.signalMeta}>
          {sig.kind && <span>{humanize(sig.kind)}</span>}
          {sig.source && (
            <>
              <span aria-hidden="true">·</span>
              <span>{sig.source}</span>
            </>
          )}
          {sig.occurred_at && (
            <>
              <span aria-hidden="true">·</span>
              <span title={signalWhenTitle(sig)}>{signalWhen(sig)}</span>
            </>
          )}
          {sig.url && (
            <>
              <span aria-hidden="true">·</span>
              <a className={styles.link} href={sig.url} target="_blank" rel="noreferrer">
                Source
              </a>
            </>
          )}
        </div>
      </div>
    </li>
  );
}

/**
 * The pre-call research dossier — the person, their company, social insights, and the buying
 * signals (each with its source), plus grounded talking points. So the SDR is fully researched
 * before they dial, with everything citable on the call. Owns its own loading/empty/error states.
 */
function BriefPanel({
  brief,
  busy,
  error,
}: {
  brief: CallBrief | null;
  busy: boolean;
  error: boolean;
}) {
  if (busy && !brief) {
    return (
      <section className={styles.briefBlock} aria-busy="true">
        <h4>Pre-call research</h4>
        <Skeleton width="100%" height={96} />
      </section>
    );
  }
  if (error && !brief) {
    return (
      <section className={styles.briefBlock}>
        <h4>Pre-call research</h4>
        <p className={styles.briefNote}>Couldn't load the research brief. The call still works.</p>
      </section>
    );
  }
  if (!brief) return null;

  const { contact, account, insights, signals, talking_points } = brief;
  const isEmpty =
    !talking_points.length && !contact && !account && !insights && !signals.length;
  if (isEmpty) {
    return (
      <section className={styles.briefBlock}>
        <h4>Pre-call research</h4>
        <p className={styles.briefNote}>
          No research yet. Run the account pipeline to enrich firmographics, signals, and contacts.
        </p>
      </section>
    );
  }

  return (
    <section className={styles.briefBlock}>
      <h4>Pre-call research</h4>

      {talking_points.length > 0 && (
        <div className={styles.tps}>
          {talking_points.map((tp, i) => (
            <p key={i} className={styles.tp}>
              {tp}
            </p>
          ))}
        </div>
      )}

      {contact && (
        <BriefSub title="Person" source={contact.source}>
          <dl className={styles.kvList}>
            {contact.role_angle && <KV label="Cares about">{contact.role_angle}</KV>}
            {(contact.title || contact.seniority) && (
              <KV label="Role">
                {[contact.title, contact.seniority && humanize(contact.seniority)]
                  .filter(Boolean)
                  .join(" · ")}
              </KV>
            )}
            {contact.email && (
              <KV label="Email">
                <a className={styles.link} href={`mailto:${contact.email}`}>
                  {contact.email}
                </a>
                {contact.email_status && (
                  <Badge tone={emailTone(contact.email_status)}>
                    {emailStatusMeta(contact.email_status).label}
                  </Badge>
                )}
              </KV>
            )}
            {contact.linkedin_url && (
              <KV label="LinkedIn">
                <a
                  className={styles.link}
                  href={contact.linkedin_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  View profile
                </a>
              </KV>
            )}
          </dl>
        </BriefSub>
      )}

      {insights && (
        <BriefSub title="Social insights" source={insights.source || undefined}>
          {insights.headline && <p className={styles.headline}>“{insights.headline}”</p>}
          {insights.recent_posts.length > 0 && (
            <ul className={styles.posts}>
              {insights.recent_posts.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
          )}
          {insights.interests.length > 0 && (
            <div className={styles.chips}>
              {insights.interests.map((it) => (
                <Badge key={it} tone="neutral">
                  {it}
                </Badge>
              ))}
            </div>
          )}
        </BriefSub>
      )}

      {account && (
        <BriefSub title="Company" source={account.source}>
          <dl className={styles.kvList}>
            <KV label="Firmographics">
              {[
                account.industry,
                account.employee_count != null
                  ? `${formatNumber(account.employee_count)} employees`
                  : null,
                account.country,
              ]
                .filter(Boolean)
                .join(" · ") || "—"}
            </KV>
            {account.domain && (
              <KV label="Website">
                <a
                  className={styles.link}
                  href={`https://${account.domain}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  {account.domain}
                </a>
              </KV>
            )}
            {account.fit_score != null && (
              <KV label="ICP fit">
                <Badge tone={strengthMeta(account.fit_score / 100).tone} dot>
                  {account.fit_score}/100
                </Badge>
                {account.fit_rationale && (
                  <span className={styles.rationale}>{account.fit_rationale}</span>
                )}
              </KV>
            )}
          </dl>
          {account.tech_stack.length > 0 && (
            <div className={styles.chips}>
              {account.tech_stack.map((t) => (
                <Badge key={t} tone="neutral">
                  {t}
                </Badge>
              ))}
            </div>
          )}
        </BriefSub>
      )}

      {signals.length > 0 && (
        <BriefSub title="Why now — signals">
          <ul className={styles.signals}>
            {signals.map((sig, i) => (
              <SignalRow key={i} sig={sig} />
            ))}
          </ul>
        </BriefSub>
      )}
    </section>
  );
}

export default CallConsole;
