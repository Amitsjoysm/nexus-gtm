import { createContext, useContext, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { EngagementStatus } from "@/lib/types";

/**
 * Whether the engagement engine is switched on, fetched once at the shell.
 *
 * `null` while unknown. This gate fails CLOSED for the new screens and OPEN for the old ones, the
 * opposite of `EntitlementsContext`, because the question is different: entitlements decide whether
 * to advertise a feature the server will police anyway, while this decides which of two engines
 * the workspace is on. Every new campaign, reply and template route answers 404 while the engine is
 * dark, so offering those links on a guess would put a rep on a page that cannot load; the old
 * Campaigns and Cadences keep working until the switch is confirmed on.
 */
const EngagementContext = createContext<EngagementStatus | null>(null);

export function useEngagementStatus(): EngagementStatus | null {
  return useContext(EngagementContext);
}

/** `true` / `false` once known (an unreadable status is `false`), `null` while loading. */
export function useEngineOn(): boolean | null {
  const status = useContext(EngagementContext);
  return status ? status.engine_on : null;
}

export function EngagementProvider({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const state = useApi<EngagementStatus>((signal) => api.engagementStatus(signal), []);
  // An unreadable status reads as OFF: the old screens work either way, the new ones only when on.
  const value = state.data ?? (state.error ? { engine_on: false, can_manage: false } : null);
  return <EngagementContext.Provider value={value}>{children}</EngagementContext.Provider>;
}

/**
 * A new-engine page renders only once the engine is confirmed on. Off sends the reader to the
 * screen that works today (the old Campaigns), with `replace` so Back does not bounce them here.
 */
export function RequireEngine({ children, fallback = "/campaigns" }: {
  children: ReactNode;
  fallback?: string;
}) {
  const on = useEngineOn();
  if (on === null) return <Skeleton width="100%" height={240} />;
  if (!on) return <Navigate to={fallback} replace />;
  return <>{children}</>;
}
