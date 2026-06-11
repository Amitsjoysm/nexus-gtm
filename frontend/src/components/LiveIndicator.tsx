import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { timeAgo } from "@/lib/format";
import styles from "./LiveIndicator.module.css";

export interface LiveIndicatorProps {
  /** Whether polling is currently active (from useLivePoll). */
  live: boolean;
  /** ms timestamp of the last refresh, or null before the first tick. */
  lastTick: number | null;
  className?: string;
}

/**
 * Status pill for a live-polling surface: a pulsing dot, a "Live"/"Paused" word, and how long
 * since the last refresh. Color is never the only signal — the word carries the state too.
 */
export function LiveIndicator({ live, lastTick, className }: LiveIndicatorProps) {
  // Tick the component every 10s so the relative "Updated Ns ago" stays honest between refreshes.
  const [, force] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => force((n) => n + 1), 10000);
    return () => window.clearInterval(t);
  }, []);

  return (
    <span className={cn(styles.wrap, className)} role="status">
      <span className={cn(styles.dot, live && styles.dotLive)} aria-hidden="true" />
      <span className={styles.label}>{live ? "Live" : "Paused"}</span>
      {lastTick != null && (
        <>
          <span className={styles.sep} aria-hidden="true">·</span>
          <span className={styles.time}>Updated {timeAgo(new Date(lastTick))}</span>
        </>
      )}
    </span>
  );
}
