import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { Spinner } from "./Spinner";
import styles from "./WorkingIndicator.module.css";

export interface WorkingIndicatorProps {
  /** What is happening, present tense, no trailing punctuation: "Searching for similar companies". */
  label: string;
  /** One honest line about the work and, where it has been measured, how long it usually takes. */
  hint?: string;
  /** Seconds after which the hint gives way to `slowHint`. */
  slowAfter?: number;
  /** Said once the wait runs long, so a slow request never looks the same as a stuck one. */
  slowHint?: string;
  className?: string;
}

const DEFAULT_SLOW_HINT = "Still working. This is taking longer than usual, but it has not stopped.";

function formatElapsed(total: number): string {
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
}

/**
 * Progress for an action that runs for seconds to a minute: Find lookalikes, Find similar people,
 * Find contacts, the AI actions.
 *
 * A 16px spinner beside static text is what got reported as "stuck". This shows three honest things
 * instead of a fabricated percentage: a bar that keeps moving, the seconds elapsed, and one line
 * about what the work is. No step names are guessed from the clock — the server does not report
 * stages, and a label that ticks along on a timer would claim progress nobody measured.
 *
 * One live region. The counter changes every second, so it is hidden from assistive tech; the
 * label is announced when this mounts, and the hint is re-announced only when it turns into the
 * "still working" note.
 */
export function WorkingIndicator({
  label,
  hint,
  slowAfter = 45,
  slowHint = DEFAULT_SLOW_HINT,
  className,
}: WorkingIndicatorProps) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    // Measured from a timestamp, not by counting ticks: a background tab throttles timers, and a
    // counter that adds one per tick would fall behind the real wait.
    const started = Date.now();
    const id = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  const note = elapsed >= slowAfter ? slowHint : hint;

  return (
    <div role="status" aria-atomic="false" className={cn(styles.root, className)}>
      <div className={styles.row}>
        <Spinner size={16} decorative />
        <span className={styles.label}>{label}…</span>
        <span className={styles.elapsed} aria-hidden="true">
          {formatElapsed(elapsed)}
        </span>
      </div>
      <span className={styles.track} aria-hidden="true">
        <span className={styles.bar} />
      </span>
      {note && <p className={styles.hint}>{note}</p>}
    </div>
  );
}
