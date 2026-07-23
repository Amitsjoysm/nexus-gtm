import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

/**
 * Global signal recency window — "show me only signals from the last N days".
 *
 * One user-chosen window applies everywhere signals render (Dashboard, Signals library,
 * Account 360), passed server-side as `max_age_days` so pagination and counts stay correct.
 * `null` means "all time". Persisted per-browser like the theme preference.
 */
export type SignalWindowDays = 7 | 15 | 30 | 60 | 90 | null;

export const SIGNAL_WINDOW_OPTIONS: { value: SignalWindowDays; label: string }[] = [
  { value: 7, label: "Last 7 days" },
  { value: 15, label: "Last 15 days" },
  { value: 30, label: "Last 30 days" },
  { value: 60, label: "Last 60 days" },
  { value: 90, label: "Last 90 days" },
  { value: null, label: "All time" },
];

const STORAGE_KEY = "nexus_signal_window";
const VALID = new Set([7, 15, 30, 60, 90]);

interface SignalWindowApi {
  /** Days back to include, or null for no window (all time). */
  windowDays: SignalWindowDays;
  setWindowDays: (d: SignalWindowDays) => void;
  /** Short label for the active window, e.g. "30d" / "All". */
  label: string;
}

const SignalWindowContext = createContext<SignalWindowApi | null>(null);

export function useSignalWindow(): SignalWindowApi {
  const ctx = useContext(SignalWindowContext);
  if (!ctx) throw new Error("useSignalWindow must be used within <SignalWindowProvider>");
  return ctx;
}

function initialWindow(): SignalWindowDays {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "all") return null;
  const n = Number(stored);
  return VALID.has(n) ? (n as SignalWindowDays) : null;
}

export function SignalWindowProvider({ children }: { children: ReactNode }) {
  const [windowDays, setState] = useState<SignalWindowDays>(initialWindow);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, windowDays === null ? "all" : String(windowDays));
  }, [windowDays]);

  const setWindowDays = useCallback((d: SignalWindowDays) => setState(d), []);
  const label = windowDays === null ? "All" : `${windowDays}d`;

  return (
    <SignalWindowContext.Provider value={{ windowDays, setWindowDays, label }}>
      {children}
    </SignalWindowContext.Provider>
  );
}
