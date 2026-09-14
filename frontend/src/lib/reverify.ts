import { emailStatusMeta } from "@/lib/display";
import type { ContactReverifyResult } from "@/lib/types";

/**
 * What re-verifying one contact did, in words a rep can act on.
 *
 * Said plainly rather than as a blanket "done": a kept address, a replaced one and a search that
 * found nothing better are three different outcomes, and only the first two are good news. `found`
 * tells the caller which toast tone fits.
 */
export function describeReverify(res: ContactReverifyResult): {
  found: boolean;
  title: string;
  body: string;
} {
  const { contact, previous_email: before, previous_status: beforeStatus } = res;
  const email = contact.email;
  const label = emailStatusMeta(contact.email_status).label;

  if (res.action === "rechecked") {
    const unchanged = beforeStatus === contact.email_status;
    return {
      found: true,
      title: "Email re-checked",
      body: `${email} is ${unchanged ? "still" : "now"} ${label}.`,
    };
  }
  if (email && email !== before) {
    return before
      ? {
          found: true,
          title: "Switched to another address",
          body: `${before} failed the check. Now using ${email} (${label}).`,
        }
      : { found: true, title: "Email found", body: `${email} (${label}).` };
  }
  if (email) {
    return { found: false, title: "No better address found", body: `${email} is ${label}.` };
  }
  return {
    found: false,
    title: "No email found",
    body: `None of the common patterns for ${contact.full_name} checked out.`,
  };
}
