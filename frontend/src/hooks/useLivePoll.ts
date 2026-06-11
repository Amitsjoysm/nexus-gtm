import { useEffect, useRef, useState } from "react";

export interface LivePoll {
  /** True while the timer is running (tab visible + enabled). Drives the "Live" indicator. */
  live: boolean;
  /** ms timestamp of the last tick fired, or null before the first. Feeds "Updated 3s ago". */
  lastTick: number | null;
}

interface Options {
  intervalMs?: number;
  enabled?: boolean;
}

/** ±10% per-tick jitter so a fleet of clients doesn't phase-lock into request spikes. */
function jittered(intervalMs: number): number {
  return Math.round(intervalMs * (0.9 + Math.random() * 0.2));
}

/**
 * Fire `onTick` on a fixed interval, but only while the tab is visible.
 *
 * A backgrounded tab pauses polling and resumes (firing once immediately to catch up) when it
 * returns to the foreground. This is the "live" mechanism for the dashboard: cheap enough to run
 * across a million tenants because hidden tabs cost nothing and each tick hits one small
 * tenant-scoped aggregate — no realtime broker, no idle background load. Each delay carries a
 * little jitter so thousands of open dashboards don't all hit the API on the same beat.
 *
 * `onTick` is read through a ref so a new closure each render doesn't tear down the interval.
 */
export function useLivePoll(onTick: () => void, { intervalMs = 12000, enabled = true }: Options = {}): LivePoll {
  const visibleNow = typeof document === "undefined" || document.visibilityState === "visible";
  const [live, setLive] = useState(enabled && visibleNow);
  const [lastTick, setLastTick] = useState<number | null>(null);
  const onTickRef = useRef(onTick);
  onTickRef.current = onTick;

  useEffect(() => {
    if (!enabled) {
      setLive(false);
      return;
    }
    let timer: number | undefined;
    let running = false;

    const fire = () => {
      onTickRef.current();
      setLastTick(Date.now());
    };
    const schedule = () => {
      timer = window.setTimeout(() => {
        fire();
        if (running) schedule();
      }, jittered(intervalMs));
    };
    const start = () => {
      if (!running) {
        running = true;
        schedule();
      }
      setLive(true);
    };
    const stop = () => {
      running = false;
      if (timer != null) {
        window.clearTimeout(timer);
        timer = undefined;
      }
      setLive(false);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        fire(); // catch up the moment the tab returns, then resume ticking
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [enabled, intervalMs]);

  return { live, lastTick };
}
