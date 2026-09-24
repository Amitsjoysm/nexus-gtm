import { useMemo } from "react";
import { Badge, Icons } from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ContactInsight, ReplyLikelihood } from "@/lib/types";
import styles from "./InsightBadge.module.css";

/**
 * How likely a person is to reply to you, and what may be said about how they reply (spec §18.5).
 *
 * The server has already applied every display rule (D26): a pattern only at three or more
 * workspaces, otherwise the last reply's speed band, and nothing for a workspace that has not
 * opted in. This component shows what it is given and never infers more. The likelihood badge says
 * its band in words and lists its reasons on hover and to screen readers, so colour is never the
 * only signal.
 */

const LIKELIHOOD: Record<Exclude<ReplyLikelihood["band"], "unknown">, { label: string; tone: BadgeTone }> = {
  high: { label: "Likely to reply", tone: "success" },
  medium: { label: "May reply", tone: "info" },
  low: { label: "Less likely to reply", tone: "neutral" },
};

/** Insights for a list of contacts, in one request (at most 100). */
export function useContactInsights(contactIds: string[]): Map<string, ContactInsight> {
  const api = useApiClient();
  const key = contactIds.slice(0, 100).join(",");
  const state = useApi<ContactInsight[]>(
    (s) => (key ? api.contactInsights(key.split(","), s) : Promise.resolve([])), [key],
  );
  return useMemo(() => new Map((state.data ?? []).map((i) => [i.contact_id, i])), [state.data]);
}

/** Whether the full (non-compact) badge would show anything for this insight. */
export function hasInsight(insight?: ContactInsight): boolean {
  if (!insight) return false;
  return insight.likelihood.band !== "unknown" || insight.person.level !== "none"
    || insight.company.level === "pattern";
}

export function InsightBadge({ insight, compact = false }: { insight?: ContactInsight; compact?: boolean }) {
  if (!insight) return null;
  const band = insight.likelihood.band;
  // Nothing to go on is said by saying nothing, not by a "less likely" badge built from no evidence.
  const like = band === "unknown" ? null : LIKELIHOOD[band];
  const why = insight.likelihood.reasons.join(". ");
  const pattern = insight.person.level !== "none" ? insight.person
    : insight.company.level === "pattern" ? insight.company : null;
  if (!like && !(pattern && !compact)) return null;
  return (
    <span className={compact ? styles.compact : styles.insight}>
      {like && <Badge tone={like.tone} title={why} aria-label={`${like.label}. ${why}`}>{like.label}</Badge>}
      {!compact && pattern && (
        <span className={styles.pattern}>
          <Icons.ActivityIcon aria-hidden className={styles.icon} />
          {pattern.text}
        </span>
      )}
    </span>
  );
}
