import { useMemo } from "react";
import { useAuth } from "@/app/AuthContext";

/**
 * The signed-in member's user id, read from the session token's `sub` claim.
 *
 * The login response carries a role and a tenant but no user id, and a session restored from storage
 * has only the token. The server builds `Principal.user_id` from this same claim, so it is the id
 * every "is this mine?" comparison has to be made against. Presentation only: whether you may claim
 * or reassign an account is decided server-side.
 */
export function userIdFromToken(token: string | null | undefined): string | null {
  const part = token?.split(".")[1];
  if (!part) return null;
  try {
    const base64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const payload = JSON.parse(atob(padded)) as { sub?: unknown };
    return typeof payload.sub === "string" && payload.sub ? payload.sub : null;
  } catch {
    // An opaque or malformed token means we cannot say what is yours, which is not the same as
    // nothing being yours. Callers treat `null` as unknown.
    return null;
  }
}

export function useCurrentUserId(): string | null {
  const { session } = useAuth();
  const token = session?.token;
  return useMemo(() => userIdFromToken(token), [token]);
}
