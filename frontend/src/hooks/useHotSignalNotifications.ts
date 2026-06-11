import { useCallback, useEffect, useRef, useState } from "react";
import type { ActivityItem } from "@/lib/types";

const STORAGE_KEY = "nexus_notify_hot_signals";

export interface HotSignalNotifications {
  /** Browser supports the Notification API. */
  supported: boolean;
  /** Opted in AND permission granted. */
  enabled: boolean;
  /** Opt in (requests permission on first enable) or out. */
  toggle: () => Promise<void>;
}

/**
 * Fire a browser notification when a NEW hot signal (strong buying signal) lands in the
 * live activity feed. Strictly opt-in: off until the rep clicks the bell, and the first
 * feed load only sets the baseline so returning to the app never replays old signals.
 * The opt-in is per browser (localStorage); permission is the browser's own grant.
 */
export function useHotSignalNotifications(items: ActivityItem[] | null): HotSignalNotifications {
  const supported = typeof window !== "undefined" && "Notification" in window;
  const [enabled, setEnabled] = useState(
    () =>
      supported &&
      localStorage.getItem(STORAGE_KEY) === "1" &&
      Notification.permission === "granted",
  );
  // Newest hot-signal timestamp already seen; only items beyond it notify.
  const lastSeen = useRef<string | null>(null);

  const toggle = useCallback(async () => {
    if (!supported) return;
    if (enabled) {
      localStorage.setItem(STORAGE_KEY, "0");
      setEnabled(false);
      return;
    }
    const perm =
      Notification.permission === "granted"
        ? "granted"
        : await Notification.requestPermission();
    if (perm === "granted") {
      localStorage.setItem(STORAGE_KEY, "1");
      setEnabled(true);
    }
  }, [enabled, supported]);

  useEffect(() => {
    if (!items || items.length === 0) return;
    const hot = items.filter((i) => i.kind === "signal" && i.tone === "success");
    if (hot.length === 0) return;
    const newest = hot[0].at;
    const prior = lastSeen.current;
    lastSeen.current = newest;
    if (prior === null || !enabled) return; // first load is baseline; disabled just tracks
    const fresh = hot.filter((i) => i.at > prior);
    // Cap the burst: three notifications max per poll, newest first.
    for (const item of fresh.slice(0, 3)) {
      try {
        new Notification(
          item.account_name ? `Hot signal: ${item.account_name}` : "Hot buying signal",
          { body: item.title, tag: item.id },
        );
      } catch {
        // Notification construction can throw (e.g. some mobile browsers); never break the app.
      }
    }
  }, [items, enabled]);

  return { supported, enabled, toggle };
}
