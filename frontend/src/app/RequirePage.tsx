import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { isPageHidden, useEntitlements } from "@/app/EntitlementsContext";

/**
 * Sends a page a superadmin has hidden to Dashboard, sub-pages included (`page` is the menu path,
 * so `/runs/:id` passes `/runs`).
 *
 * A redirect rather than an "unavailable" screen, unlike a feature switch: a hidden page is one the
 * product does not offer, so there is nothing to explain and nothing to wait for. It fails open like
 * `RequireCapability`, and it hides a page only; the endpoints behind it keep answering, because
 * taking a feature away is the module switch's job.
 */
export function RequirePage({ page, children }: { page: string; children: ReactNode }) {
  const entitlements = useEntitlements();
  if (isPageHidden(entitlements, page)) return <Navigate to="/dashboard" replace />;
  return <>{children}</>;
}
