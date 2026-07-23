/** Maps domain values to UI tones/labels. Keeps screens consistent. */
import type { BadgeTone } from "@/components/ui";
import { humanize } from "./format";
import type { AlertSeverity, SignalEvent } from "./types";

/** Activity-feed tone string → Badge tone ("critical" maps to the danger pill). */
export function activityTone(tone: string): BadgeTone {
  switch (tone) {
    case "critical":
      return "danger";
    case "warning":
      return "warning";
    case "success":
      return "success";
    case "info":
      return "info";
    default:
      return "neutral";
  }
}

export function severityTone(severity: AlertSeverity): BadgeTone {
  switch (severity) {
    case "critical":
      return "danger";
    case "warning":
      return "warning";
    default:
      return "info";
  }
}

/** Signal/relevance strength (0..1) → tone + label. */
export function strengthMeta(strength: number): { tone: BadgeTone; label: string } {
  if (strength >= 0.75) return { tone: "success", label: "Strong" };
  if (strength >= 0.45) return { tone: "warning", label: "Medium" };
  return { tone: "neutral", label: "Weak" };
}

// --- signal provenance ------------------------------------------------------
// Sources that fabricate signals so the pipeline runs without live feeds. These
// are NOT real-world events, so we mark them and never present them as verified.
const SYNTHETIC_SIGNAL_SOURCES = new Set(["demo", "stub"]);

// Friendly names for known machine source keys; anything else is humanized.
const SIGNAL_SOURCE_LABELS: Record<string, string> = {
  demo: "Demo",
  stub: "Demo",
  web_news: "Web news",
  g2_intent: "G2",
  crm: "CRM",
  csv: "CSV import",
  play: "Playbook",
};

const SYNTHETIC_SIGNAL_HINT =
  "Sample signal generated for demonstration — not a confirmed real-world event. " +
  "In production, demo signals are turned off so only verified sources feed the pipeline.";

export interface SignalSourceMeta {
  /** Human-readable source label, e.g. "Demo", "Web news", "G2". */
  label: string;
  /** True when the signal was synthesized (demo/stub), not observed for real. */
  isSynthetic: boolean;
  /** Tooltip explaining provenance so a rep knows how much to trust it. */
  hint: string;
  /** A link a rep can open to confirm the signal — real source URL or a web search. */
  href: string;
  /** Link text: "View source" when we have the origin, else "Search the web". */
  linkLabel: string;
  /** True when `href` is the signal's own source URL (a real, citable link). */
  verified: boolean;
}

/** Turn a demo signal's title into a focused web query so the rep can go confirm it. */
function verifyQuery(sig: SignalEvent): string {
  return sig.title
    .trim()
    .replace(/\s+is hiring in a relevant function$/i, " careers jobs hiring")
    .replace(/\s+announced new funding$/i, " funding round raised");
}

/**
 * Provenance + a confirm link for a signal, so an SDR can always click through to
 * verify it. Real sources link to their origin URL; synthetic (demo) signals get a
 * web-search link and are flagged as unverified rather than shown as confirmed.
 */
export function signalSourceMeta(sig: SignalEvent): SignalSourceMeta {
  const isSynthetic = SYNTHETIC_SIGNAL_SOURCES.has(sig.source);
  const label = SIGNAL_SOURCE_LABELS[sig.source] ?? humanize(sig.source);
  if (sig.url) {
    return {
      label,
      isSynthetic,
      hint: isSynthetic ? SYNTHETIC_SIGNAL_HINT : `Detected via ${label}. Open the source to confirm.`,
      href: sig.url,
      linkLabel: "View source",
      verified: true,
    };
  }
  const href = `https://www.google.com/search?q=${encodeURIComponent(verifyQuery(sig))}`;
  return {
    label,
    isSynthetic,
    hint: isSynthetic
      ? SYNTHETIC_SIGNAL_HINT
      : `Detected via ${label}. No direct link on file — search the web to confirm.`,
    href,
    linkLabel: "Search the web",
    verified: false,
  };
}

/** Email-verification verdict → tone + label. Mirrors the backend statuses (valid/risky/
 *  invalid/unknown); null/blank means we have an address but no verdict yet. */
export function emailStatusMeta(
  status: string | null | undefined,
): { tone: BadgeTone; label: string } {
  switch ((status ?? "").toLowerCase()) {
    case "valid":
      return { tone: "success", label: "valid" };
    case "risky":
      return { tone: "warning", label: "risky" };
    case "invalid":
      return { tone: "danger", label: "invalid" };
    case "unknown":
      return { tone: "neutral", label: "unknown" };
    default:
      return { tone: "neutral", label: "unverified" };
  }
}

/** Inbox priority (lower number = more urgent in the backend) → tone. */
export function priorityTone(priority: number): BadgeTone {
  if (priority <= 1) return "danger";
  if (priority <= 3) return "warning";
  return "neutral";
}

/** Campaign lifecycle status → tone. */
export function campaignTone(status: string): BadgeTone {
  switch (status) {
    case "completed":
      return "success";
    case "awaiting_approval":
      return "warning";
    case "failed":
      return "danger";
    case "drafting":
    case "approved":
    case "sending":
      return "info";
    default:
      return "neutral"; // draft_pending, cancelled
  }
}

/** Campaign target status → tone. */
export function targetTone(status: string): BadgeTone {
  switch (status) {
    case "sent":
      return "success";
    case "drafted":
    case "approved":
      return "info";
    case "skipped":
      return "warning";
    case "failed":
      return "danger";
    default:
      return "neutral"; // pending, drafting
  }
}

/** Cadence enrollment status → tone. */
export function enrollmentTone(status: string): BadgeTone {
  switch (status) {
    case "active":
      return "success";
    case "paused":
      return "warning";
    case "stopped":
      return "danger";
    default:
      return "neutral"; // completed
  }
}

/** Cadence touch status → tone. */
export function touchTone(status: string): BadgeTone {
  switch (status) {
    case "sent":
      return "success";
    case "awaiting_approval":
      return "warning";
    case "failed":
      return "danger";
    case "skipped":
      return "neutral";
    default:
      return "neutral";
  }
}
