import type { ReactNode } from "react";
import {
  AlertTriangleIcon,
  BellIcon,
  BoltIcon,
  BuildingIcon,
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
  { to: "/settings", label: "Settings", icon: <SettingsIcon />, minRole: "admin" },
];

export function canSee(item: NavItem, role: Role | undefined): boolean {
  if (!item.minRole) return true;
  if (!role) return false;
  return ROLE_RANK[role] >= ROLE_RANK[item.minRole];
}

export const ALERT_NAV_ICON = <AlertTriangleIcon />;
