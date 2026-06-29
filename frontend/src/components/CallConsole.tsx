import { useEffect, useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  Icons,
  Input,
  Skeleton,
  Textarea,
  useToast,
} from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { CALL_DISPOSITIONS } from "@/lib/types";
import type {
  CallBrief,
  CallBriefSignal,
  CallScript,
  CallTask,
} from "@/lib/types";
import { formatNumber, humanize, timeAgo } from "@/lib/format";
import { strengthMeta } from "@/lib/display";
import styles from "./CallConsole.module.css";

const DISPO_LABEL: Record<string, string> = {
  connected: "Connected",
  voicemail: "Voicemail",
  no_answer: "No answer",
  callback: "Callback",
  meeting_booked: "Meeting booked",
  not_interested: "Not interested",
  bad_number: "Bad number",
  gatekeeper: "Gatekeeper",
};

function telHref(phone: string | null): string | null {
  if (!phone) return null;
  const digits = phone.replace(/[^\d+]/g, "");
  return digits ? `tel:${digits}` : null;
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

  async function generate() {
    setScriptBusy(true);
    try {
      setScript(await api.generateCallScript(task.id));
    } catch (err) {
      toast.error("Couldn't generate script", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setScriptBusy(false);
    }
  }

  // Reset + (optionally) pre-generate whenever the call changes.
  useEffect(() => {
    setScript(null);
    setNotes("");
    setNextStep("");
    if (autoGenerate) void generate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id]);

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
      });
      toast.success("Logged", `${DISPO_LABEL[disposition] ?? disposition} — ${task.contact_name ?? "contact"}.`);
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
        {dial ? (
          <a className={styles.dialBtn} href={dial}>
            <Icons.PhoneIcon /> Call {task.phone}
          </a>
        ) : (
          <Badge tone="warning" dot>No number</Badge>
        )}
      </header>

      {task.reason && <p className={styles.reason}>{task.reason}</p>}

      <BriefPanel brief={brief} busy={briefBusy} error={briefError} />

      <div className={styles.scriptBlock}>
        <div className={styles.scriptHead}>
          <h4>AI call script</h4>
          <Button size="sm" variant="secondary" loading={scriptBusy} onClick={generate}>
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
        <div className={styles.dispoGrid}>
          {CALL_DISPOSITIONS.map((d) => (
            <Button
              key={d}
              size="sm"
              variant={d === "meeting_booked" ? "primary" : "secondary"}
              loading={logging === d}
              disabled={!!logging}
              onClick={() => logDisposition(d)}
            >
              {DISPO_LABEL[d] ?? d}
            </Button>
          ))}
        </div>
      </div>
    </div>
  );
}

/** Deliverability verdict → badge tone for the contact's email. */
function emailTone(status: string | null): "success" | "warning" | "danger" | "neutral" {
  switch (status) {
    case "valid":
      return "success";
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
              <span>{timeAgo(sig.occurred_at)}</span>
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
                  <Badge tone={emailTone(contact.email_status)}>{contact.email_status}</Badge>
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
