import { createContext, useContext, type ReactNode } from "react";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { Entitlements } from "@/lib/types";

/**
 * The workspace's module gates, fetched once per session and shared.
 *
 * Fetched at the shell so navigation, page headers and empty states all answer "is this included
 * in our plan?" from one request instead of each issuing its own.
 */
const EntitlementsContext = createContext<Entitlements | null>(null);

export function useEntitlements(): Entitlements | null {
  return useContext(EntitlementsContext);
}

/**
 * Whether a capability should be treated as NOT available to this workspace.
 *
 * Two conditions, and the second is the one that matters:
 *
 * 1. the plan does not include it, AND
 * 2. the server is actually enforcing (`gating_active`).
 *
 * `NEXUS_BILLING_ENFORCEMENT` defaults to `shadow`, which resolves every entitlement and then
 * allows the call regardless. Gating the UI on the policy alone would hide features that still
 * work perfectly — a shadow-mode rollout, whose entire promise is that it changes nothing, would
 * suddenly become a visible product regression. So the UI never claims something is unavailable
 * that the server would happily serve.
 *
 * Missing/in-flight entitlements resolve to "available", matching the engine's own bias: unknown
 * always means allow, and a nav bar that empties out while a request is in flight is worse than
 * one that briefly offers a link.
 */
export function isLocked(entitlements: Entitlements | null, capabilityId?: string): boolean {
  if (!capabilityId || !entitlements || !entitlements.gating_active) return false;
  const module = entitlements.modules.find((m) => m.capability_id === capabilityId);
  if (!module) return false;
  return !module.included;
}

export function EntitlementsProvider({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const state = useApi<Entitlements>((signal) => api.billingEntitlements(signal), []);
  // An error deliberately yields `null`, which `isLocked` reads as "nothing is locked". Failing
  // OPEN is right here and is the opposite of `RequirePlatformAdmin`: that gate protects staff
  // tooling, whereas this one only decides whether to advertise a feature the server will police
  // anyway. A billing endpoint blip must not delete the customer's navigation.
  return (
    <EntitlementsContext.Provider value={state.data ?? null}>
      {children}
    </EntitlementsContext.Provider>
  );
}
