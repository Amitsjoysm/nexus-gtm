import { createContext, useContext, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { EmptyState, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { PlatformIdentity } from "@/lib/types";

/**
 * The identity the gate already fetched, shared with everything it wraps.
 *
 * Without this, every panel that needs to know "can I do X?" would issue its own `whoami` — the
 * gate has already paid for that request.
 */
const PlatformIdentityContext = createContext<PlatformIdentity | null>(null);

export function usePlatformIdentity(): PlatformIdentity | null {
  return useContext(PlatformIdentityContext);
}

/**
 * `can(permission)` for the current staff user.
 *
 * Defaults to FALSE when the identity is missing, so a failed or in-flight `whoami` hides
 * controls rather than offering ones the server will refuse.
 */
export function usePlatformCan(): (permission: string) => boolean {
  const identity = usePlatformIdentity();
  return (permission: string) => (identity?.permissions ?? []).includes(permission);
}

/**
 * Gate for the staff console.
 *
 * Platform admin is NOT a tenant role. Guarding this route on `minRole="owner"` (as it was) let
 * any workspace owner load the console shell — the API refused every call, so no data leaked,
 * but a page that renders for people who cannot use it reads as broken access control.
 *
 * The server is still the only real boundary (`require_platform_permission`); this just stops the
 * SPA offering a door that will not open.
 */
export function RequirePlatformAdmin({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const identity = useApi<PlatformIdentity>((signal) => api.platformWhoAmI(signal), []);

  if (identity.loading && identity.data == null) {
    return <Skeleton width="100%" height={240} />;
  }

  // Treat an error the same as "not an admin": failing closed is the only safe default here.
  if (identity.error || !identity.data?.is_platform_admin) {
    return (
      <EmptyState
        title="Staff access only"
        description="The billing control plane is limited to platform administrators. Workspace roles, including owner, do not grant access."
      />
    );
  }

  return (
    <PlatformIdentityContext.Provider value={identity.data}>
      {children}
    </PlatformIdentityContext.Provider>
  );
}

/** Redirect helper for routes that should not even render a message. */
export function PlatformAdminOnly({ children }: { children: ReactNode }) {
  const api = useApiClient();
  const identity = useApi<PlatformIdentity>((signal) => api.platformWhoAmI(signal), []);

  if (identity.loading && identity.data == null) return null;
  if (!identity.data?.is_platform_admin) return <Navigate to="/dashboard" replace />;
  return (
    <PlatformIdentityContext.Provider value={identity.data}>
      {children}
    </PlatformIdentityContext.Provider>
  );
}
