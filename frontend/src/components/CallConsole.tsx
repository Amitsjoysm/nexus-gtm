import { useEffect, useState } from "react";
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
import type { CallScript, CallTask } from "@/lib/types";
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

export default CallConsole;
