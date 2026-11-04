import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  Badge,
  Button,
  Field,
  Icons,
  Input,
  Select,
  Skeleton,
  SkeletonText,
  Textarea,
  useToast,
} from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type {
  CallActivity,
  CallTaskCampaignScript,
  DispositionCategory,
  CallBrief,
  CallBriefSignal,
  CallScript,
  CallTask,
  TelephonyStatus,
} from "@/lib/types";
import { formatDate, formatNumber, humanize, signalWhen, signalWhenTitle, timeAgo } from "@/lib/format";
import { strengthMeta } from "@/lib/display";
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

/** A past call's outcome, toned by its category so a column of them reads at a glance. The label
 *  always says the outcome too: colour is never the only signal. */
const CATEGORY_TONE: Record<string, BadgeTone> = {
  positive: "success",
  follow_up: "info",
  negative: "danger",
  compliance: "danger",
  contact_issues: "warning",
};

/** The first-reaction lines a call script carries ("who's this?", "send me an email", "I'm busy").
 *  Read when the server sends them; a script written before they existed simply has none. */
type ScriptWithReactions = CallScript & {
  first_reactions?: { reaction: string; response: string }[];
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

/**
 * The one-screen console's own size (6.3). It is laid out as a fixed grid only where it fits:
 * a desktop viewport, with the app's sidebar beside it. Below that it stacks and the page scrolls,
 * which beats squeezing a script into a 200px column. Mirrored by the `.screen` media query.
 */
const SCREEN_QUERY = "(min-width: 1200px) and (min-height: 640px)";
/** Never shorter than this, or the panels stop being readable; past it the page scrolls instead. */
const MIN_SCREEN_HEIGHT = 560;
/** Space kept under the console, between its last row and the bottom of the window. */
const SCREEN_GAP = 16;

/**
 * Size the console to exactly the space left in the content scroller, so nothing on it scrolls the
 * page. Measured rather than written as `100dvh - ...`, because what sits above it (the topbar, an
 * impersonation banner, the frame's padding) is not this component's to know. The frame's bottom
 * padding is pulled back with a negative margin, or it alone would add a scrollbar.
 */
function useFitToScroller(ref: RefObject<HTMLElement>, active: boolean) {
  const [box, setBox] = useState<{ height: number; pull: number } | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    const scroller = el?.closest("main");
    if (!active || !el || !scroller) {
      setBox(null);
      return;
    }
    const media = window.matchMedia(SCREEN_QUERY);
    const frame = scroller.firstElementChild as HTMLElement | null;
    scroller.scrollTo({ top: 0 });
    const measure = () => {
      if (!media.matches) {
        setBox(null);
        return;
      }
      const padBottom = frame ? parseFloat(getComputedStyle(frame).paddingBottom) || 0 : 0;
      const top = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top
        + scroller.scrollTop;
      const height = Math.floor(scroller.clientHeight - top - SCREEN_GAP) - 1;
      setBox({ height: Math.max(MIN_SCREEN_HEIGHT, height), pull: Math.max(0, padBottom - SCREEN_GAP) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    media.addEventListener("change", measure);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", measure);
    };
  }, [ref, active]);
  return box;
}

/** The current time, re-read every `everyMs`, for clocks that must keep moving. */
function useNow(everyMs: number, active = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs, active]);
  return now;
}

function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export interface CallConsoleProps {
  task: CallTask;
  /** Auto-generate the AI script as soon as the task is shown (great for the account modal). */
  autoGenerate?: boolean;
  /** Called after a disposition is logged (close the modal / refresh the queue). */
  onLogged?: (disposition: string) => void;
  /**
   * "stacked" (default): one column, for the Call modal on an account or a contact.
   * "screen": the Calls page's console, one fixed grid sized to the window with nothing to scroll
   * on the page (6.3), and keys for the call: C call, E end, 1-9 outcome, Ctrl+Enter log, Esc queue.
   */
  layout?: "stacked" | "screen";
  /** Screen layout: leave the call for the queue (Esc). */
  onExit?: () => void;
}

/**
 * The full single-call workspace: click-to-dial, the AI talk track, and one-tap disposition
 * logging. Shared by the Calls power-list and the per-contact "Call" modal on an account, so the
 * SDR gets the exact same calling experience wherever they start the call.
 *
 * On the Calls page it is ONE screen (6.3, 2026-10-29): the rep is prepared before the call and
 * never scrolls during it. The person and how to reach them across the top; who they are and what
 * happened before on the left; the campaign's own script in the middle, read as written; the AI's
 * talking points, where our value props meet their likely pains, and what to say when they push
 * back on the right; the outcome along the bottom. Long text scrolls inside its own panel.
 */
export function CallConsole({ task, autoGenerate, onLogged, layout = "stacked", onExit }: CallConsoleProps) {
  const api = useApiClient();
  const toast = useToast();
  const screen = layout === "screen";
  const rootRef = useRef<HTMLDivElement>(null);
  const fit = useFitToScroller(rootRef, screen);
  const [script, setScript] = useState<ScriptWithReactions | null>(null);
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
  // Calls already made to this person, newest first (`/calling/contacts/{id}/activities`).
  const [history, setHistory] = useState<CallActivity[] | null>(null);
  const [historyError, setHistoryError] = useState(false);
  // When the rep started and ended this call, for the clock in the header. Shown, never sent: the
  // minutes a platform call is charged for are Twilio's measured ones.
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [endedAt, setEndedAt] = useState<number | null>(null);
  const onCall = startedAt !== null && endedAt === null;
  const now = useNow(1000, onCall);
  const dialLinkRef = useRef<HTMLAnchorElement>(null);
  const notesRef = useRef<HTMLTextAreaElement>(null);
  const nameId = useId();
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
    setStartedAt(null);
    setEndedAt(null);
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

  // What happened on earlier calls to this person: the rep should not open as a stranger to
  // someone who said "call me in June" last time.
  useEffect(() => {
    let active = true;
    const ctrl = new AbortController();
    setHistory(null);
    setHistoryError(false);
    if (!task.contact_id) {
      setHistory([]);
      return;
    }
    api
      .contactCallActivities(task.contact_id, ctrl.signal)
      .then((rows) => active && setHistory(rows))
      .catch(() => active && setHistoryError(true));
    return () => {
      active = false;
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.contact_id]);

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
      setStartedAt(Date.now());
      setEndedAt(null);
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

  /** C: dial. Live telephony places the bridge; otherwise the `tel:` link opens the rep's phone. */
  function startCall() {
    if (!dial || onCall || dialing) return;
    if (isLive) {
      void dialLive();
      return;
    }
    dialLinkRef.current?.click();
  }

  /** E: the call is over (the rep hangs up on their phone); stop the clock and go to the notes. */
  function endCall() {
    if (!onCall) return;
    setEndedAt(Date.now());
    notesRef.current?.focus();
  }

  /** 1-9: a category, then a disposition inside it. Backspace goes back to the categories. */
  function pickByNumber(n: number) {
    if (!category) {
      const target = catalog[n - 1];
      if (target) pickCategory(target.key);
      return;
    }
    const target = inCategory[n - 1];
    if (target) {
      setDispositionCode(target.code);
      setFollowUpAt("");
    }
  }

  function submit() {
    if (chosen && !logging) void logDisposition(chosen.code);
  }

  // The keys read the latest state through a ref, so the listener is registered once per call.
  const keys = useRef({ startCall, endCall, pickByNumber, submit, pickCategory, category, onExit });
  keys.current = { startCall, endCall, pickByNumber, submit, pickCategory, category, onExit };
  useEffect(() => {
    if (!screen) return;
    function onKey(e: KeyboardEvent) {
      if (e.defaultPrevented || e.repeat) return;
      const k = keys.current;
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        k.submit();
        return;
      }
      const target = e.target instanceof Element ? e.target : null;
      // Typing in a field, or a dialog open over the console, owns the keyboard.
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      if (document.querySelector('[role="dialog"]')) return;
      const key = e.key.toLowerCase();
      if (key === "c") {
        e.preventDefault();
        k.startCall();
      } else if (key === "e") {
        e.preventDefault();
        k.endCall();
      } else if (/^[1-9]$/.test(key)) {
        e.preventDefault();
        k.pickByNumber(Number(key));
      } else if (key === "backspace" && k.category) {
        e.preventDefault();
        k.pickCategory("");
      } else if (key === "escape" && k.onExit) {
        e.preventDefault();
        k.onExit();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [screen]);

  const dispositionLabels = useMemo(() => {
    const map = new Map<string, { label: string; category: string }>();
    for (const c of catalog) for (const d of c.dispositions) map.set(d.code, { label: d.label, category: c.key });
    return map;
  }, [catalog]);

  const NameHeading = screen ? "h2" : "h3";
  const numbered = (i: number, label: string) => (screen ? `${i + 1}. ${label}` : label);

  const dialControl = !dial ? (
    <Badge tone="warning" dot>No number</Badge>
  ) : onCall ? (
    <>
      <span className={styles.timer} role="timer" aria-live="off">
        <span className={styles.liveDot} aria-hidden="true" />
        On call {clock(now - (startedAt ?? now))}
      </span>
      <Button variant="secondary" onClick={endCall}>
        End call{screen && <kbd className={styles.kbd}>E</kbd>}
      </Button>
    </>
  ) : isLive ? (
    <Button onClick={dialLive} loading={dialing} disabled={dialing} iconLeft={<Icons.PhoneIcon />}>
      Start call{screen && <kbd className={styles.kbd}>C</kbd>}
    </Button>
  ) : (
    <a
      ref={dialLinkRef}
      className={styles.dialBtn}
      href={dial}
      onClick={() => {
        setStartedAt(Date.now());
        setEndedAt(null);
      }}
    >
      <Icons.PhoneIcon /> Call {task.phone}
      {screen && <kbd className={styles.kbd}>C</kbd>}
    </a>
  );

  const aiScriptBody = (
    <>
      {scriptBusy && !script && <SkeletonText lines={6} />}
      {!scriptBusy && !script && (
        <p className={styles.note}>No script yet. Generate one for this person.</p>
      )}
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
          <p><strong>Ask.</strong> {script.cta}</p>
          <p className={styles.vm}><strong>Voicemail.</strong> {script.voicemail}</p>
        </div>
      )}
    </>
  );

  const scriptActions = (
    <span className={styles.scriptActions}>
      {script?.generated_at && (
        <span className={styles.panelAside}>
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
    </span>
  );

  const ownScriptPanel = ownScript && (
    // The team's own script, read out as written (D10), with the AI's track under it to borrow from.
    <Panel
      area="script"
      scroll={screen}
      title="Your script"
      aside={<span className={styles.panelAside}>{ownScript.campaign_name}</span>}
    >
      <pre className={styles.ownScript}>{ownScript.text}</pre>
      <section className={styles.reference} aria-label="AI talk track, for reference">
        <div className={styles.referenceHead}>
          <h5 className={styles.subTitle}>AI talk track, for reference</h5>
          {scriptActions}
        </div>
        {aiScriptBody}
      </section>
    </Panel>
  );

  return (
    <div
      ref={rootRef}
      className={cn(styles.console, screen ? styles.screen : styles.stacked)}
      style={fit ? { height: fit.height, marginBottom: -fit.pull } : undefined}
      aria-labelledby={nameId}
      role={screen ? "region" : undefined}
    >
      <header className={styles.head}>
        <div className={styles.who}>
          <NameHeading id={nameId} className={styles.callName}>
            {task.contact_name ?? "Unknown contact"}
          </NameHeading>
          <p className={styles.callMeta}>
            {task.title ? `${task.title} · ` : ""}{task.account_name}
          </p>
        </div>
        <ul className={styles.facts} aria-label="Reaching them">
          <li><LocalTime zone={brief?.local_timezone} source={brief?.timezone_source} loading={briefBusy && !brief} /></li>
          {task.phone && (
            <li className={styles.phone}>
              <Icons.PhoneIcon aria-hidden="true" /> {task.phone}
            </li>
          )}
          {brief?.contact?.linkedin_url && (
            <li>
              <a className={styles.link} href={brief.contact.linkedin_url} target="_blank" rel="noreferrer">
                <Icons.ExternalLinkIcon aria-hidden="true" /> LinkedIn
              </a>
            </li>
          )}
        </ul>
        <div className={styles.callActions}>
          {dialControl}
          {onExit && (
            <Button variant="ghost" iconLeft={<Icons.ChevronLeftIcon />} onClick={onExit}>
              Queue{screen && <kbd className={styles.kbd}>Esc</kbd>}
            </Button>
          )}
        </div>
        {dial && isLive && (
          <div className={styles.bridge}>
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
          </div>
        )}
      </header>

      <Panel area="them" scroll={screen} title="Who they are">
        <ThemBody task={task} brief={brief} busy={briefBusy} error={briefError} />
      </Panel>

      <Panel area="past" scroll={screen} title="Past calls">
        <PastCalls
          history={history}
          error={historyError}
          firstName={(task.contact_name ?? "").split(" ")[0] || "them"}
          labels={dispositionLabels}
        />
      </Panel>

      {ownScriptPanel ?? (
        <Panel area="script" scroll={screen} title="AI call script" aside={scriptActions}>
          {aiScriptBody}
        </Panel>
      )}

      <Panel area="talk" scroll={screen} title="Where we help">
        <ValueFit brief={brief} busy={briefBusy} />
        <TalkingPoints brief={brief} busy={briefBusy} />
      </Panel>

      <Panel area="reply" scroll={screen} title="If they say…">
        <IfTheySay script={script} busy={scriptBusy} />
      </Panel>

      <section className={styles.log} aria-label="Log the outcome">
        {!screen && <h4 className={styles.logTitle}>Log the outcome</h4>}
        <div className={styles.logFields}>
          <Field label="Notes" className={styles.notesField}>
            <Textarea
              ref={notesRef}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="What they said, in a line or two"
              rows={2}
            />
          </Field>
          <Field label="Next step" className={styles.nextField}>
            <Input
              value={nextStep}
              onChange={(e) => setNextStep(e.target.value)}
              placeholder="e.g. demo Tue 2pm"
            />
          </Field>
          <Field label="Category" className={styles.pickField}>
            <Select
              value={category}
              onChange={(e) => pickCategory(e.target.value)}
              placeholder="Choose a category"
              options={catalog.map((c, i) => ({ value: c.key, label: numbered(i, c.label) }))}
              disabled={!catalog.length || !!logging}
            />
          </Field>
          <Field label="Disposition" className={styles.pickField}>
            <Select
              value={dispositionCode}
              onChange={(e) => {
                setDispositionCode(e.target.value);
                setFollowUpAt("");
              }}
              placeholder={category ? "Choose an outcome" : "Choose a category first"}
              options={inCategory.map((d, i) => ({ value: d.code, label: numbered(i, d.label) }))}
              disabled={!category || !!logging}
            />
          </Field>
          {chosen?.needs_date && (
            <Field label="Follow up on" className={styles.pickField}>
              <Input
                type="datetime-local"
                value={followUpAt}
                onChange={(e) => setFollowUpAt(e.target.value)}
              />
            </Field>
          )}
          <Button
            variant="primary"
            className={styles.logBtn}
            loading={!!logging}
            disabled={!chosen || !!logging}
            onClick={submit}
          >
            Log outcome
          </Button>
        </div>
        <div className={styles.logFoot}>
          <p className={styles.dispoHint} role="status">
            {chosen && ACTION_HINT[chosen.action]
              ? ACTION_HINT[chosen.action]
              : chosen?.needs_date
                ? "Optional date. The call stays in your queue either way."
                : ""}
          </p>
          {screen && (
            <p className={styles.keys}>
              <kbd className={styles.kbd}>C</kbd> call <kbd className={styles.kbd}>E</kbd> end{" "}
              <kbd className={styles.kbd}>1</kbd>–<kbd className={styles.kbd}>9</kbd> outcome{" "}
              <kbd className={styles.kbd}>Ctrl</kbd>+<kbd className={styles.kbd}>Enter</kbd> log
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

/** A titled panel of the console. On the Calls page its body scrolls on its own, never the page;
 *  the scroller takes keyboard focus so it can be read without a mouse. */
function Panel({
  area,
  title,
  aside,
  scroll,
  children,
}: {
  area: "them" | "past" | "script" | "talk" | "reply";
  title: string;
  aside?: ReactNode;
  scroll: boolean;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className={cn(styles.panel, styles[area])} aria-labelledby={id}>
      <div className={styles.panelHead}>
        <h4 id={id} className={styles.panelTitle}>{title}</h4>
        {aside}
      </div>
      <div className={styles.panelBody} tabIndex={scroll ? 0 : undefined}
        role={scroll ? "group" : undefined} aria-labelledby={scroll ? id : undefined}>
        {children}
      </div>
    </section>
  );
}

/**
 * Their local time, with a word when it is outside a working day there. From the contact's own
 * timezone, else the account's location; when neither says, it says so rather than guess.
 */
function LocalTime({ zone, source, loading }: { zone?: string | null; source?: string | null; loading: boolean }) {
  const now = useNow(30_000, Boolean(zone));
  if (loading) return <Skeleton width={150} height={14} />;
  if (!zone) {
    return (
      <span className={styles.factMuted}>
        <Icons.ClockIcon aria-hidden="true" /> Local time unknown
      </span>
    );
  }
  let time = "";
  let off = false;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, hour: "numeric", hourCycle: "h23", weekday: "short",
    }).formatToParts(now);
    const hour = Number(parts.find((p) => p.type === "hour")?.value ?? 12);
    const weekday = parts.find((p) => p.type === "weekday")?.value ?? "Mon";
    off = weekday === "Sat" || weekday === "Sun" || hour < 8 || hour >= 18;
    time = new Intl.DateTimeFormat([], { timeZone: zone, hour: "numeric", minute: "2-digit" }).format(now);
  } catch {
    return (
      <span className={styles.factMuted}>
        <Icons.ClockIcon aria-hidden="true" /> Local time unknown
      </span>
    );
  }
  const place = zone.split("/").pop()?.replace(/_/g, " ");
  const from = source === "contact" ? "their own timezone" : "where the company is";
  return (
    <span className={styles.localTime} title={`${zone}, from ${from}`}>
      <Icons.ClockIcon aria-hidden="true" />
      <span>
        <strong>{time}</strong> their time{place ? ` · ${place}` : ""}
      </span>
      {off && <Badge tone="warning">Outside 8am–6pm</Badge>}
    </span>
  );
}

/** Why this call, the account facts worth knowing, the signals worth mentioning, and what the
 *  person says about themselves. Every block carries its source. */
function ThemBody({
  task,
  brief,
  busy,
  error,
}: {
  task: CallTask;
  brief: CallBrief | null;
  busy: boolean;
  error: boolean;
}) {
  if (busy && !brief) return <SkeletonText lines={7} />;
  const account = brief?.account ?? null;
  const insights = brief?.insights ?? null;
  const signals = (brief?.signals ?? []).slice(0, 3);
  const size = account?.employee_count != null ? `${formatNumber(account.employee_count)} employees` : null;
  // Firmographics in one line: a glance, not a form. The signals below are what gets said.
  const facts = account ? [account.industry, size, account.country].filter(Boolean).join(" · ") : "";
  return (
    <div className={styles.stack}>
      {task.reason && <p className={styles.reason}><strong>Why this call.</strong> {task.reason}</p>}
      {error && !brief && (
        <p className={styles.note}>Couldn't load the research brief. The call still works.</p>
      )}
      {account && (facts || account.domain) && (
        <p className={styles.factsLine}>
          {facts}
          {account.domain && (
            <>
              {facts && <span aria-hidden="true"> · </span>}
              <a className={styles.link} href={`https://${account.domain}`} target="_blank" rel="noreferrer">
                {account.domain}
              </a>
            </>
          )}
        </p>
      )}
      {account?.fit_score != null && (
        <p className={styles.fitLine}>
          <Badge tone={strengthMeta(account.fit_score / 100).tone} dot>ICP fit {account.fit_score}/100</Badge>
          {account.fit_rationale && <span className={styles.rationale}>{account.fit_rationale}</span>}
        </p>
      )}
      {signals.length > 0 && (
        <section className={styles.sub} aria-label="Worth mentioning">
          <h5 className={styles.subTitle}>Worth mentioning</h5>
          <ul className={styles.signals}>
            {signals.map((sig, i) => <SignalRow key={i} sig={sig} />)}
          </ul>
        </section>
      )}
      {account?.source && <p className={styles.source}>Company data: {account.source}</p>}
      {insights?.headline && (
        <section className={styles.sub} aria-label="In their words">
          <h5 className={styles.subTitle}>In their words</h5>
          <p className={styles.headline}>“{insights.headline}”</p>
          {insights.source && <p className={styles.source}>Source: {insights.source}</p>}
        </section>
      )}
      {brief && !account && !signals.length && !insights && !task.reason && (
        <p className={styles.note}>
          No research yet. Refresh the account to fill its firmographics and signals.
        </p>
      )}
    </div>
  );
}

function SignalRow({ sig }: { sig: CallBriefSignal }) {
  const meta = strengthMeta(sig.strength);
  return (
    <li className={styles.signal}>
      <div className={styles.signalTitle}>
        {sig.title}
        {sig.is_personal && <span className={styles.personTag}>About them</span>}
      </div>
      <div className={styles.signalMeta}>
        <Badge tone={meta.tone} dot>{meta.label}</Badge>
        {sig.kind && <span>{humanize(sig.kind)}</span>}
        {sig.occurred_at && (
          <>
            <span aria-hidden="true">·</span>
            <span title={signalWhenTitle(sig)}>{signalWhen(sig)}</span>
          </>
        )}
        {sig.url ? (
          <>
            <span aria-hidden="true">·</span>
            <a className={styles.link} href={sig.url} target="_blank" rel="noreferrer">
              {sig.source || "Source"}
            </a>
          </>
        ) : sig.source ? (
          <>
            <span aria-hidden="true">·</span>
            <span>{sig.source}</span>
          </>
        ) : null}
      </div>
    </li>
  );
}

/** Earlier calls to this person, newest first: when, the outcome, and what was noted. */
function PastCalls({
  history,
  error,
  firstName,
  labels,
}: {
  history: CallActivity[] | null;
  error: boolean;
  firstName: string;
  labels: Map<string, { label: string; category: string }>;
}) {
  if (error) return <p className={styles.note}>Couldn't load earlier calls.</p>;
  if (history === null) return <SkeletonText lines={3} />;
  if (!history.length) return <p className={styles.note}>First call to {firstName}.</p>;
  return (
    <ol className={styles.history}>
      {history.map((a) => {
        const known = labels.get(a.disposition);
        return (
          <li key={a.id} className={styles.historyItem}>
            <div className={styles.historyHead}>
              <Badge tone={CATEGORY_TONE[known?.category ?? ""] ?? "neutral"} dot>
                {known?.label ?? humanize(a.disposition)}
              </Badge>
              <span className={styles.historyWhen} title={new Date(a.occurred_at).toLocaleString()}>
                {formatDate(a.occurred_at)} · {timeAgo(a.occurred_at)}
              </span>
            </div>
            {a.notes && <p className={styles.historyNote}>{a.notes}</p>}
            {a.next_step && <p className={styles.historyNext}>Next: {a.next_step}</p>}
          </li>
        );
      })}
    </ol>
  );
}

/** The brief's grounded talking points, built only from real data. */
function TalkingPoints({ brief, busy }: { brief: CallBrief | null; busy: boolean }) {
  const points = brief?.talking_points ?? [];
  if ((busy && !brief) || !points.length) return null;
  return (
    <section className={styles.sub} aria-label="Talking points">
      <h5 className={styles.subTitle}>Talking points</h5>
      <ul className={styles.tps}>
        {points.map((tp, i) => <li key={i} className={styles.tp}>{tp}</li>)}
      </ul>
    </section>
  );
}

/**
 * Where our value props meet what this person likely cares about. The pains are the workspace's
 * own (`value_props[].pains_solved`); a tick marks one that shares words with their title, role or
 * signals. That is a likely match, worded as one, never a claim about them.
 */
function ValueFit({ brief, busy }: { brief: CallBrief | null; busy: boolean }) {
  if (busy && !brief) return <SkeletonText lines={5} />;
  if (!brief) return <p className={styles.note}>Couldn't load the research brief.</p>;
  const props = brief.value_fit ?? [];
  return (
    <section className={styles.sub} aria-label="Our value props and their likely pains">
      {brief.likely_pain && (
        <p className={styles.likely}>
          {brief.contact?.title ? `As ${brief.contact.title}, they` : "They"} likely care
          about {brief.likely_pain}.
        </p>
      )}
      {props.length > 0 ? (
        <ul className={styles.props}>
          {props.map((vp) => (
            <li key={vp.name} className={styles.prop}>
              <span className={styles.propName}>{vp.name}</span>
              {vp.description && <span className={styles.propDesc}>{vp.description}</span>}
              {vp.pains.length > 0 && (
                <ul className={styles.pains} aria-label={`Pains ${vp.name} solves`}>
                  {vp.pains.map((p) => {
                    const theirs = vp.matched.includes(p);
                    return (
                      <li key={p} className={cn(styles.pain, theirs && styles.painTheirs)}>
                        {theirs && <Icons.CheckIcon aria-hidden="true" />}
                        {p}
                        {theirs && <span className="sr-only"> (likely theirs)</span>}
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.note}>Add value props in Relevance to see where they meet this person.</p>
      )}
    </section>
  );
}

/** What to say when they push back: first reactions, then the objections the script prepared. */
function IfTheySay({ script, busy }: { script: ScriptWithReactions | null; busy: boolean }) {
  if (busy && !script) return <SkeletonText lines={4} />;
  const reactions = script?.first_reactions ?? [];
  const objections = script?.objections ?? [];
  if (!reactions.length && !objections.length) {
    return <p className={styles.note}>The AI script lists objections and replies once it is written.</p>;
  }
  return (
    <dl className={styles.replies}>
      {[...reactions.map((r) => ({ said: r.reaction, reply: r.response })),
        ...objections.map((o) => ({ said: o.objection, reply: o.response }))].map((r, i) => (
        <div key={i} className={styles.replyItem}>
          <dt className={styles.said}>“{r.said}”</dt>
          <dd className={styles.reply}>{r.reply}</dd>
        </div>
      ))}
    </dl>
  );
}

export default CallConsole;
