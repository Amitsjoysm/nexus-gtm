import { useAuth } from "@/app/AuthContext";
import type { Role } from "@/lib/types";

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

/**
 * Can this member connect a workspace alert channel, and decide what it receives for the team?
 *
 * `PUT /alert-connections/{kind}` and its `/rules` are `manage_alert_channels`: manager and up,
 * decided with the product owner. A workspace has ONE Slack, so a rep replacing it would redirect
 * the whole team's alerts, while a team lead setting up the channel their team works from is
 * normal. Choosing which of your own alerts go there is rep-level and lives in the notification
 * preferences.
 *
 * Used to show a rep what is missing and who to ask, rather than a form that 403s on submit.
 */
export function useCanConnectChannels(): boolean {
  const { session } = useAuth();
  return session ? ROLE_RANK[session.role] >= ROLE_RANK.manager : false;
}
