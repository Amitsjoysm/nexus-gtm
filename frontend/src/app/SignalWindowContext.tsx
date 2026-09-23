import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import type { SignalWindowOption, SignalWindowPolicy } from "@/lib/types";

/**
 * Global signal recency window: "show me only signals from the last N days".
 *
 * One window applies everywhere signals render (Dashboard, Signals, Account 360, Inbox, Alerts),
 * passed server-side as `max_age_days` so pagination and counts stay correct. `null` is all time.
 *
 * A superadmin owns the policy (`GET /signals/window`, set in the Control plane):
 * - `user_choice` on: each user picks, starting from the platform default, remembered per browser.
 * - `user_choice` off: the default applies to everyone and the picker becomes a read-only label.
 *   The server enforces it too, so this is presentation, not the control.
 *
 * Until the policy arrives, or if it cannot be read, the user's own choice stands with the full
 * list. Failing open matches the server: a window it cannot read hides nothing.
 */
export type SignalWindowDays = number | null;

/** The same list the server serves, so the picker works before the policy loads. */
export const FALLBACK_WINDOW_OPTIONS: SignalWindowOption[] = [
  { label: "Weekly", days: 7 },
  { label: "Fortnightly", days: 14 },
  { label: "Monthly", days: 30 },
  { label: "Quarterly", days: 90 },
  { label: "Half-yearly", days: 180 },
  { label: "Yearly", days: 365 },
  { label: "All time", days: null },
];

interface SignalWindowApi {
  /** Days back to include, or null for all time. What every list sends. */
  windowDays: SignalWindowDays;
  /** Ignored while a superadmin has taken the choice. */
  setWindowDays: (d: SignalWindowDays) => void;
  options: SignalWindowOption[];
  /** False when a superadmin has set one window for everyone. */
  userChoice: boolean;
  /** "Monthly", "All time". */
  label: string;
}

const SignalWindowContext = createContext<SignalWindowApi | null>(null);

export function useSignalWindow(): SignalWindowApi {
  const ctx = useContext(SignalWindowContext);
  if (!ctx) throw new Error("useSignalWindow must be used within <SignalWindowProvider>");
  return ctx;
}

const STORAGE_KEY = "nexus_signal_window";

/** The stored choice: a number of days, "all", or nothing chosen yet (undefined). */
function storedChoice(): SignalWindowDays | undefined {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === null) return undefined;
  if (stored === "all") return null;
  const n = Number(stored);
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

export function SignalWindowProvider({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const { session } = useAuth();
  const signedIn = Boolean(session);
  const policy = useApi<SignalWindowPolicy | null>(
    (signal) => (signedIn ? api.signalWindow(signal) : Promise.resolve(null)),
    [signedIn],
  );
  const [chosen, setChosen] = useState<SignalWindowDays | undefined>(storedChoice);

  useEffect(() => {
    if (chosen === undefined) return;
    localStorage.setItem(STORAGE_KEY, chosen === null ? "all" : String(chosen));
  }, [chosen]);

  const value = useMemo<SignalWindowApi>(() => {
    const p = policy.data;
    const options = p?.options?.length ? p.options : FALLBACK_WINDOW_OPTIONS;
    const userChoice = p ? p.user_choice : true;
    const fallback = p ? p.default_days : null;
    // A stored choice counts only if it is still on the list: "15" and "60" from the old picker
    // fall back to the default rather than sending a window no screen can show.
    const valid = chosen !== undefined && options.some((o) => o.days === chosen);
    const windowDays = !userChoice ? fallback : valid ? (chosen as SignalWindowDays) : fallback;
    const label = options.find((o) => o.days === windowDays)?.label ?? `${windowDays} days`;
    return {
      windowDays,
      setWindowDays: (d) => {
        if (userChoice) setChosen(d);
      },
      options,
      userChoice,
      label,
    };
  }, [policy.data, chosen]);

  return <SignalWindowContext.Provider value={value}>{children}</SignalWindowContext.Provider>;
}

/** Exact days for a tooltip: "the last 30 days", "all time". */
export function describeWindow(days: SignalWindowDays): string {
  return days === null ? "all time" : `the last ${days} days`;
}
