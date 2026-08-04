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
}

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

export const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: <DashboardIcon /> },
  { to: "/inbox", label: "Inbox", icon: <InboxIcon /> },
  { to: "/calls", label: "Calls", icon: <PhoneIcon /> },
  { to: "/accounts", label: "Accounts", icon: <BuildingIcon /> },
  { to: "/contacts", label: "Contacts", icon: <UsersIcon /> },
  { to: "/network", label: "Network", icon: <NetworkIcon /> },
  { to: "/lists", label: "Lists", icon: <ListIcon /> },
  { to: "/signals", label: "Signals", icon: <SignalIcon /> },
  { to: "/alerts", label: "Alerts", icon: <BellIcon /> },
  { to: "/orchestrator", label: "Orchestrator", icon: <SparklesIcon />, minRole: "manager" },
  { to: "/runs", label: "AI Runs", icon: <WorkflowIcon />, minRole: "manager" },
  { to: "/approvals", label: "Approvals", icon: <ShieldCheckIcon />, minRole: "manager" },
  { to: "/campaigns", label: "Campaigns", icon: <SendIcon />, minRole: "manager" },
  { to: "/cadences", label: "Cadences", icon: <MessageIcon />, minRole: "manager" },
  { to: "/plays", label: "Plays", icon: <BoltIcon />, minRole: "manager" },
  { to: "/relevance", label: "Relevance", icon: <TargetIcon />, minRole: "manager" },
  { to: "/members", label: "Members", icon: <UsersIcon />, minRole: "manager" },
  { to: "/integrations", label: "Integrations", icon: <PlugIcon />, minRole: "admin" },
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

export const ALERT_NAV_ICON = <AlertTriangleIcon />;
