import { createContext, useContext, type ReactNode } from "react";
import { Skeleton } from "@/components/ui";
import { FeatureUnavailable } from "@/components/FeatureUnavailable";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { EngagementStatus } from "@/lib/types";

/**
 * Whether the engagement engine is switched on, fetched once at the shell.
 *
 * Since the cutover (spec §13) the engine is the only campaign engine, on by default, and switching
 * it off is the platform's emergency stop for sending. So this now fails OPEN, like
 * `EntitlementsContext`: an unreadable status reads as on, because the server is the boundary (every
 * engagement route answers 404 while the engine is off) and hiding the only campaign screens on a
 * network blip would take away an SDR's daily driver. Before the cutover it failed closed, because
 * the old Campaigns and Cadences worked either way and were the safe place to send people.
 */
const EngagementContext = createContext<EngagementStatus | null>(null);

export function useEngagementStatus(): EngagementStatus | null {
  return useContext(EngagementContext);
}

/** `true` / `false` once known (an unreadable status is `true`), `null` while loading. */
export function useEngineOn(): boolean | null {
  const status = useContext(EngagementContext);
  return status ? status.engine_on : null;
}

export function EngagementProvider({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const state = useApi<EngagementStatus>((signal) => api.engagementStatus(signal), []);
  const value = state.data ?? (state.error ? { engine_on: true, can_manage: false } : null);
  return <EngagementContext.Provider value={value}>{children}</EngagementContext.Provider>;
}

/**
 * An engagement page renders unless the engine is confirmed off. Off is said in place, keeping the
 * URL, so the page comes back where the reader left it when the engine is switched on again; there
 * is no other campaign screen to send them to.
 */
export function RequireEngine({ children, name = "Campaigns" }: {
  children: ReactNode;
  name?: string;
}) {
  const on = useEngineOn();
  if (on === null) return <Skeleton width="100%" height={240} />;
  if (!on) {
    return (
      <FeatureUnavailable
        name={name} state="maintenance"
        message="Campaigns, replies and sequence templates are switched off for now. Nothing is being sent or read from your mailbox."
      />
    );
  }
  return <>{children}</>;
}
