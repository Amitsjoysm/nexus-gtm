import { useAuth } from "@/app/AuthContext";
import type { Role } from "@/lib/types";

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

/**
 * Can this member connect a workspace alert channel?
 *
 * `PUT /alert-connections/{kind}` is `manage_workspace`, which is owner and admin. A workspace has
 * ONE Slack, so connecting it is a workspace decision; choosing which of your own alerts go there
 * is rep-level and lives in the notification preferences.
 *
 * Used to show a rep what is missing and who to ask, rather than a form that 403s on submit.
 */
export function useCanConnectChannels(): boolean {
  const { session } = useAuth();
  return session ? ROLE_RANK[session.role] >= ROLE_RANK.admin : false;
}
