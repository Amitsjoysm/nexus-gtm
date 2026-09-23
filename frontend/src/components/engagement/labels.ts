import type { BadgeTone } from "@/components/ui";
import type {
  EngagementCampaignStatus,
  EngagementEnrollmentStatus,
  ReplyCategory,
} from "@/lib/types";

/**
 * The words the engagement screens use for states the server names in snake_case. One map per
 * vocabulary, so the campaign list, the detail page and the reply desk cannot drift into three
 * spellings of "waiting for review".
 */

export const CAMPAIGN_STATUS: Record<EngagementCampaignStatus, { label: string; tone: BadgeTone }> = {
  draft: { label: "Setting up", tone: "neutral" },
  reviewing: { label: "In review", tone: "info" },
  active: { label: "Running", tone: "success" },
  paused: { label: "Paused", tone: "warning" },
  completed: { label: "Finished", tone: "neutral" },
};

export const ENROLLMENT_STATUS: Record<EngagementEnrollmentStatus, { label: string; tone: BadgeTone }> = {
  awaiting_review: { label: "Awaiting review", tone: "info" },
  active: { label: "In sequence", tone: "success" },
  paused: { label: "Paused", tone: "warning" },
  snoozed: { label: "Scheduled", tone: "accent" },
  stopped: { label: "Stopped", tone: "neutral" },
  completed: { label: "Finished", tone: "neutral" },
};

/** Why an enrollment is in its state, as a sentence fragment. Unknown reasons fall back to "". */
const REASONS: Record<string, string> = {
  replied: "they replied",
  colleague_replied: "a colleague replied",
  needs_decision: "their reply needs your decision",
  out_of_office: "out of office",
  later: "asked to hear back later",
  manual: "by hand",
  bounced: "the address bounced",
  unsubscribed: "they unsubscribed",
  declined: "they declined",
  out_of_credits: "the workspace ran out of credits",
  mailbox_disconnected: "the sending mailbox was disconnected",
};

export function reasonText(reason: string | null | undefined): string {
  if (!reason) return "";
  if (reason.startsWith("do_not_contact")) return "on the do-not-contact list";
  return REASONS[reason] ?? reason.replace(/_/g, " ");
}

export const CATEGORY: Record<ReplyCategory, { label: string; tone: BadgeTone }> = {
  interested: { label: "Interested", tone: "success" },
  question: { label: "Question", tone: "info" },
  referral: { label: "Referral", tone: "accent" },
  later: { label: "Later", tone: "neutral" },
  out_of_office: { label: "Out of office", tone: "neutral" },
  declined: { label: "Declined", tone: "warning" },
  unsubscribe: { label: "Unsubscribe", tone: "danger" },
  unclear: { label: "Needs a decision", tone: "warning" },
};

export const CATEGORY_OPTIONS = (Object.keys(CATEGORY) as ReplyCategory[]).map((value) => ({
  value,
  label: CATEGORY[value].label,
}));

/** Capability ids on the launch estimate, as the line a customer recognises on their bill. */
export const CAPABILITY_LINE: Record<string, string> = {
  "ai.email_draft": "Writing each email",
  "outreach.email_send": "Sending each email",
  "ai.reply_classify": "Reading each reply",
  "ai.reply_draft": "Suggesting each answer",
};

export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

/** A timestamp as the reader's own short date and time, or an em-less placeholder. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "Not scheduled";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Not scheduled";
  return d.toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

export function whenDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}
