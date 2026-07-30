/**
 * Platform-admin permission names, mirroring `nexus/billing/permissions.py`.
 *
 * These are stable strings stored in the database and quoted in audit rows, so they are safe to
 * hardcode here. The console uses them to hide controls a narrower role cannot use — the server
 * enforces every one of them regardless, so a stale copy of this file weakens nothing.
 */
export const BILLING_READ = "billing.read";
export const PRICING_WRITE = "pricing.write";
export const SUBSCRIPTIONS_WRITE = "subscriptions.write";
export const CREDITS_GRANT = "credits.grant";
export const CREDITS_GRANT_CAPPED = "credits.grant.capped";
export const INVOICES_COLLECT = "invoices.collect";
export const JOBS_MANAGE = "jobs.manage";
export const ADMINS_MANAGE = "admins.manage";
export const USERS_MANAGE = "users.manage";

/** Human labels for the permission chips shown against each admin. */
export const PERMISSION_LABELS: Record<string, string> = {
  [BILLING_READ]: "Read billing",
  [PRICING_WRITE]: "Edit pricing",
  [SUBSCRIPTIONS_WRITE]: "Manage subscriptions",
  [CREDITS_GRANT]: "Grant credits",
  [CREDITS_GRANT_CAPPED]: "Grant credits (capped)",
  [INVOICES_COLLECT]: "Collect invoices",
  [JOBS_MANAGE]: "Manage jobs",
  [ADMINS_MANAGE]: "Manage admins",
  [USERS_MANAGE]: "Manage users",
};

export function permissionLabel(name: string): string {
  return PERMISSION_LABELS[name] ?? name;
}
