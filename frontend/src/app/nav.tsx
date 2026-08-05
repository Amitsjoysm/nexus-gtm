import type { ReactNode } from "react";
import {
  AlertTriangleIcon,
  BellIcon,
  BoltIcon,
  BuildingIcon,
  CreditCardIcon,
  DashboardIcon,
  InboxIcon,
  ListIcon,
  MessageIcon,
  NetworkIcon,
  PhoneIcon,
  PlugIcon,
  SendIcon,
  SettingsIcon,
  ShieldCheckIcon,
  SignalIcon,
  SparklesIcon,
  TargetIcon,
  UsersIcon,
  WorkflowIcon,
} from "@/components/ui/icons";
import type { Role } from "@/lib/types";

export interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  /** Minimum role required to see the item (omit = everyone). */
  minRole?: Role;
  /**
   * Visible only to PLATFORM admins (staff), never to a workspace member however senior.
   *
   * This exists because `minRole` cannot express it. Platform admin is deliberately a separate
   * authorization system from tenant RBAC — no workspace role grants it — so there was no way to
   * say "show this to staff" in a model that only knows rep/manager/admin/owner. The consequence
   * was that the billing control plane had no navigation entry at all: it worked, and the only way
   * to reach it was to know the URL.
   */
  platformOnly?: boolean;
  /**
   * Module capability this item belongs to. When the plan does not include it AND the server is
   * actually enforcing, the item is either hidden or shown locked — see `navState` below.
   *
   * Only coarse `module.*` gates belong here. Per-action quotas (how many drafts are left) are
   * the action's business, not navigation's: a menu that greys out at 19 of 20 emails would be
   * lying about a feature the customer still has.
   */
  capability?: string;
}

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

export const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: <DashboardIcon /> },
  { to: "/inbox", label: "Inbox", icon: <InboxIcon /> },
  { to: "/calls", label: "Calls", icon: <PhoneIcon />, capability: "module.calling" },
  { to: "/accounts", label: "Accounts", icon: <BuildingIcon /> },
  { to: "/contacts", label: "Contacts", icon: <UsersIcon /> },
  { to: "/network", label: "Network", icon: <NetworkIcon />, capability: "module.network" },
  { to: "/lists", label: "Lists", icon: <ListIcon /> },
  { to: "/signals", label: "Signals", icon: <SignalIcon /> },
  { to: "/alerts", label: "Alerts", icon: <BellIcon /> },
  { to: "/orchestrator", label: "Orchestrator", icon: <SparklesIcon />, minRole: "manager" },
  { to: "/runs", label: "AI Runs", icon: <WorkflowIcon />, minRole: "manager" },
  { to: "/approvals", label: "Approvals", icon: <ShieldCheckIcon />, minRole: "manager" },
  {
    to: "/campaigns", label: "Campaigns", icon: <SendIcon />, minRole: "manager",
    capability: "module.outreach",
  },
  {
    to: "/cadences", label: "Cadences", icon: <MessageIcon />, minRole: "manager",
    capability: "module.outreach",
  },
  { to: "/plays", label: "Plays", icon: <BoltIcon />, minRole: "manager" },
  { to: "/relevance", label: "Relevance", icon: <TargetIcon />, minRole: "manager" },
  { to: "/members", label: "Members", icon: <UsersIcon />, minRole: "manager" },
  {
    to: "/integrations", label: "Integrations", icon: <PlugIcon />, minRole: "admin",
    capability: "module.integrations",
  },
  { to: "/settings/billing", label: "Billing", icon: <CreditCardIcon />, minRole: "admin" },
  { to: "/settings", label: "Settings", icon: <SettingsIcon />, minRole: "admin" },
  // Staff-only, and last: the control plane is operator tooling, not part of the product a
  // workspace member is working in.
  {
    to: "/admin/billing",
    label: "Control plane",
    icon: <ShieldCheckIcon />,
    platformOnly: true,
  },
];

export function canSee(item: NavItem, role: Role | undefined, isPlatformAdmin = false): boolean {
  // A platform-only item is invisible to every workspace member, including an owner. The server
  // enforces this regardless — the nav entry only decides whether the link is offered.
  if (item.platformOnly) return isPlatformAdmin;
  if (!item.minRole) return true;
  if (!role) return false;
  return ROLE_RANK[role] >= ROLE_RANK[item.minRole];
}

/** How a nav item should render given the plan. */
export type NavState = "visible" | "locked" | "hidden";

/** Roles that can actually do something about a locked module (i.e. change the plan). */
const CAN_UPGRADE: Role[] = ["admin", "owner"];

/**
 * Hide the item, or show it with an upgrade prompt? **Both — decided by who is looking.**
 *
 * The two options are usually argued as a single global choice, and each is half right:
 *
 * * Hiding is cleaner and produces no dead ends, but a workspace on a small plan then has no way
 *   to discover that Network or Campaigns exist. The upsell surface is exactly zero.
 * * Showing a locked item is the upsell, but to someone who cannot act on it, it is a permanent
 *   advertisement for something they are not allowed to buy. A rep cannot change the plan, so
 *   for them a padlock is noise on every page load, forever.
 *
 * The variable neither framing accounts for is **agency**. `admin` and `owner` can change the
 * plan, so for them a locked item is actionable and is the whole point. `rep` and `manager`
 * cannot, so for them it is clutter with no path forward, and hiding is kinder.
 *
 * This is not a compromise bolted on to avoid choosing: navigation here is already role-dependent
 * (`minRole` hides most of the menu from a rep today), so "different people see different items"
 * is the established model rather than a new concept introduced by billing.
 *
 * To make it uniform instead, change this one function: `return "locked"` for the upsell
 * everywhere, or `return "hidden"` to always hide. Nothing else needs to move.
 */
export function navState(role: Role | undefined, locked: boolean): NavState {
  if (!locked) return "visible";
  return role && CAN_UPGRADE.includes(role) ? "locked" : "hidden";
}

export const ALERT_NAV_ICON = <AlertTriangleIcon />;
