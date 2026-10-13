import type { McpScope } from "@/lib/types";

/** The order permissions are always shown in: least to most. */
export const SCOPES: McpScope[] = ["read", "research", "write"];

/**
 * What each permission lets a connected app do, in the words the consent screen and the AI apps
 * page both use. One definition, so the screen where someone grants access and the screen where
 * they review it can never describe the same permission two ways.
 */
export const SCOPE_COPY: Record<McpScope, { label: string; detail: string }> = {
  read: {
    label: "Read",
    detail: "See accounts, contacts, signals, lists, campaigns, your inbox and the credit balance.",
  },
  research: {
    label: "Research",
    detail:
      "Find contacts, find similar companies, enrich contacts, research accounts and draft emails. Spends the workspace's credits, at the prices the app charges.",
  },
  write: {
    label: "Write",
    detail:
      "Add accounts, create and fill lists, add people to campaigns, complete inbox tasks and save emails to your Drafts. Never sends an email.",
  },
};

export function sortScopes(scopes: readonly string[]): McpScope[] {
  return SCOPES.filter((s) => scopes.includes(s));
}
