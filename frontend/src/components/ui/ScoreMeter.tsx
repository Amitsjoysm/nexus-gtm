import { cn } from "@/lib/cn";
import type { BadgeTone } from "./Badge";
import styles from "./ScoreMeter.module.css";

export interface ScoreMeterProps {
  /** Fit score, 0–100 (clamped). */
  value: number;
  className?: string;
}

/**
 * Horizontal fit-score bar. Tone steps with the value (green ≥70, amber ≥40, red below)
 * and is exposed as an ARIA meter so the number is announced, not just shown.
 */
export function ScoreMeter({ value, className }: ScoreMeterProps) {
  const pct = Math.max(0, Math.min(100, value));
  const tone: BadgeTone = pct >= 70 ? "success" : pct >= 40 ? "warning" : "danger";
  return (
    <div
      className={cn(styles.meter, className)}
      role="meter"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      data-tone={tone}
    >
      <span className={styles.fill} style={{ width: `${pct}%` }} />
    </div>
  );
}
