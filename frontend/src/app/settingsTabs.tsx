import {
  BellIcon,
  BuildingIcon,
  CreditCardIcon,
  LinkIcon,
  MailIcon,
  MessageIcon,
  SendIcon,
  UsersIcon,
} from "@/components/ui/icons";
import type { NavItem } from "./nav";

/**
 * The tabs of Settings, and the one list every reader of them uses: the tab menu
 * (`SettingsLayout`), the page titles (`AppShell.titleFor`) and the superadmin's page switch
 * (`nexus/features/pages.py`, pinned to this list by `tests/test_page_visibility.py`).
 *
 * Asked for 2026-10-13: My mailboxes, AI apps and Members used to be separate items in the main
 * menu beside an admin-only Settings, so a rep had three places to look for things that are all
 * "my setup", and Settings itself was a single long page. They are tabs now, in three groups that
 * answer "whose setting is this?": yours, your team's, the workspace's.
 *
 * A tab reuses the menu's own rules (`canSee`, `navState`, `isPageHidden`): `minRole` hides it
 * from roles that cannot use it, `capability` follows the plan and the platform switches, `engine`
 * leaves with the engagement engine, and `page` names what a superadmin hides it with (a tab that
 * moved here from elsewhere keeps the switch it had: Notifications goes with Alerts).
 */
export interface SettingsTab extends NavItem {
  group: SettingsGroup;
  /** The top bar's title, where the tab's own label would be ambiguous on its own there. */
  title?: string;
  /** The page-visibility path that hides this tab. Defaults to `to`. */
  page?: string;
}

export type SettingsGroup = "you" | "team" | "workspace";

export const SETTINGS_GROUPS: { id: SettingsGroup; label: string }[] = [
  { id: "you", label: "You" },
  { id: "team", label: "Team" },
  { id: "workspace", label: "Workspace" },
];

export const SETTINGS_TABS: SettingsTab[] = [
  {
    to: "/settings/mailboxes", label: "My mailboxes", icon: <MailIcon />, group: "you",
    capability: "module.outreach",
  },
  {
    to: "/settings/ai-apps", label: "AI apps", icon: <LinkIcon />, group: "you",
    capability: "module.mcp",
  },
  {
    to: "/settings/notifications", label: "Notifications", icon: <BellIcon />, group: "you",
    capability: "module.signals", page: "/alerts",
  },
  {
    to: "/settings/replies", label: "Replies", icon: <MessageIcon />, group: "team",
    capability: "module.campaigns", engine: "on", page: "/engagement/replies",
    title: "Reply settings",
  },
  {
    to: "/settings/members", label: "Members", icon: <UsersIcon />, group: "team",
    minRole: "manager",
  },
  {
    to: "/settings/workspace", label: "Workspace", icon: <BuildingIcon />, group: "workspace",
    minRole: "admin", title: "Workspace settings",
  },
  {
    to: "/settings/email", label: "Email & signature", icon: <SendIcon />, group: "workspace",
    minRole: "admin",
  },
  {
    to: "/settings/billing", label: "Billing", icon: <CreditCardIcon />, group: "workspace",
    minRole: "admin",
  },
];

/** The address a superadmin hides the tab with. */
export function tabPage(tab: SettingsTab): string {
  return tab.page ?? tab.to;
}
