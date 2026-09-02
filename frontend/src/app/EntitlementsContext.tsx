import { createContext, useContext, type ReactNode } from "react";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { Entitlements, FeatureSwitchState } from "@/lib/types";

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
  if (!capabilityId || !entitlements) return false;
  const module = entitlements.modules.find((m) => m.capability_id === capabilityId);
  if (!module) return false;
  // `locked` is computed by the server, which already folds in both rules: a PLAN gate locks only
  // when `gating_active`, a PLATFORM SWITCH locks always. Deriving that here would put the same
  // three-way condition in the client, where this function and `RequireCapability` are two
  // readers of it — exactly the shape two callers get subtly different.
  //
  // `?? !module.included` covers a server that predates the field during a rolling deploy. Kept
  // guarded by `gating_active` so the old behaviour is reproduced exactly, rather than a mixed
  // version of the two.
  return module.locked ?? (entitlements.gating_active && !module.included);
}

/**
 * The platform switch on a capability, if a switch is what took it away.
 *
 * `null` when the module is available, or when it is locked by the PLAN rather than by a switch —
 * those two cases need opposite screens. A plan gate is an upsell and routes to billing; a switch
 * is our decision, and offering to sell someone a feature we have turned off ourselves is the
 * failure this distinction exists to prevent.
 */
export function switchNotice(
  entitlements: Entitlements | null,
  capabilityId?: string,
): { state: FeatureSwitchState; message: string } | null {
  if (!capabilityId || !entitlements) return null;
  const module = entitlements.modules.find((m) => m.capability_id === capabilityId);
  if (!module?.switch_state || module.switch_state === "enabled") return null;
  return { state: module.switch_state, message: module.switch_message };
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
