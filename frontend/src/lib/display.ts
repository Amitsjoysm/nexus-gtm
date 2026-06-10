/** Maps domain values to UI tones/labels. Keeps screens consistent. */
import type { BadgeTone } from "@/components/ui";
import type { AlertSeverity } from "./types";

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

/** Inbox priority (lower number = more urgent in the backend) → tone. */
export function priorityTone(priority: number): BadgeTone {
  if (priority <= 1) return "danger";
  if (priority <= 3) return "warning";
  return "neutral";
}
