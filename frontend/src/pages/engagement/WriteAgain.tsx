import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Badge, Button, EmptyState, ErrorState, Field, Icons, Input, Skeleton, Textarea, WorkingIndicator,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { LikelihoodBadge } from "@/components/engagement/InsightBadge";
import { whenDay } from "@/components/engagement/labels";
import type { AsyncState } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { RestartSuggestion } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Signal re-engagement (spec §19): people who went quiet, or asked for later, at a company where
 * something has happened since. The server decides who is fair to write to (never someone who
 * declined or unsubscribed, never twice for one piece of news); this lists them and drafts an
 * email in the same thread. Nothing is sent until Send.
 */

const REASON: Record<RestartSuggestion["reason"], string> = {
  quiet: "Went quiet",
  later: "Asked for later",
};

export function WriteAgain({ state, onChanged }: {
  state: AsyncState<RestartSuggestion[]>;
  onChanged: () => void;
}) {
  if (state.error) {
    return <ErrorState title="Couldn't load suggestions" message={state.error.detail} onRetry={state.refetch} />;
  }
  if (!state.data) {
    return (
      <div className={styles.stack}>
        {[0, 1, 2].map((i) => <Skeleton key={i} width="100%" height={96} />)}
      </div>
    );
  }
  if (state.data.length === 0) {
    return (
      <EmptyState icon={<Icons.SignalIcon />} title="Nobody to write to again yet"
        description="When something happens at a company whose people went quiet or asked for later, they appear here with a reason to get back in touch." />
    );
  }
  return (
    <ul className={styles.suggestions} aria-label="People worth writing to again">
      {state.data.map((s) => (
        <li key={`${s.enrollment_id}-${s.signal_id}`}>
          <Suggestion s={s} onSent={onChanged} />
        </li>
      ))}
    </ul>
  );
}

function Suggestion({ s, onSent }: { s: RestartSuggestion; onSent: () => void }) {
  const api = useApiClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<"draft" | "send" | null>(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const titleId = `restart-${s.enrollment_id}`;

  async function draft() {
    setOpen(true);
    setBusy("draft");
    try {
      const fresh = await api.restartDraft(s.enrollment_id, s.signal_id);
      setSubject(fresh.subject);
      setBody(fresh.body);
      if (fresh.quality_problems.length) toast.error("Check the draft", fresh.quality_problems.join("; "));
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  async function send() {
    setBusy("send");
    try {
      const result = await api.restartSend(s.enrollment_id, s.signal_id, { subject, body });
      if (result.outcome !== "sent") throw new ApiError(409, `Not sent: ${result.reason || result.outcome}.`);
      toast.success("Sent", `In the same thread as your earlier emails to ${s.contact_name}.`);
      onSent();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className={styles.reviewCard} aria-labelledby={titleId}>
      <header className={styles.suggestionHead}>
        <div className={styles.person}>
          <h3 id={titleId} className={styles.personName}>{s.contact_name}</h3>
          <span className={styles.muted}>
            {s.account_name}
            {" · "}
            <Link to={`/engagement/campaigns/${s.campaign_id}`}>{s.campaign_name}</Link>
          </span>
        </div>
        <span className={styles.badgeRow}>
          <Badge tone="neutral">{REASON[s.reason]}</Badge>
          <LikelihoodBadge band={s.likelihood} />
        </span>
      </header>
      <p className={styles.signalLine}>
        <Icons.SignalIcon aria-hidden className={styles.inlineIcon} />
        <span className={styles.signalText}>
          <span>{s.signal_title}</span>
          <time className={styles.muted} dateTime={s.signal_at}>{whenDay(s.signal_at)}</time>
        </span>
      </p>

      {open && (busy === "draft" ? (
        <WorkingIndicator label="Reading the thread and writing the email" hint="Drafts usually take a few seconds." />
      ) : (
        <>
          <Field label="Subject">
            <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
          </Field>
          <Field label="Email" hint="Your signature is added from your mailbox when it sends.">
            <Textarea rows={7} value={body} onChange={(e) => setBody(e.target.value)} />
          </Field>
        </>
      ))}

      <div className={styles.formActions}>
        <Button variant="secondary" iconLeft={<Icons.SparklesIcon />} onClick={draft}
          disabled={busy !== null} loading={busy === "draft"}>
          {body ? "Draft again" : "Draft email"}
        </Button>
        {open && (
          <Button iconLeft={<Icons.SendIcon />} onClick={send}
            disabled={busy !== null || !subject.trim() || !body.trim()} loading={busy === "send"}>
            Send
          </Button>
        )}
      </div>
    </article>
  );
}
