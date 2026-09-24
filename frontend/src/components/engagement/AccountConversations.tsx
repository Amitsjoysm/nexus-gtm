import { Card, ErrorState, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { Contact, ReplyCategory, TimelineEntry } from "@/lib/types";
import { ConversationTimeline } from "./ConversationTimeline";
import { InsightBadge, hasInsight, useContactInsights } from "./InsightBadge";
import styles from "./AccountConversations.module.css";

/**
 * Every email to and from the people at an account, across campaigns and one-off sends (spec §9,
 * "a cross-campaign conversation timeline"), and above it what may be said about how each of them
 * replies (§18.5: insights appear on the contact page). Previews, oldest first; the full thread of a
 * reply is on the reply desk.
 */
export function AccountConversations({ accountId }: { accountId: string }) {
  const api = useApiClient();
  const timeline = useApi<TimelineEntry[]>(
    (s) => api.engagementTimeline({ account_id: accountId }, s), [accountId],
  );
  const contacts = useApi<Contact[]>((s) => api.listContacts(accountId, s), [accountId]);
  const withEmail = (contacts.data ?? []).filter((c) => c.email);
  const insights = useContactInsights(withEmail.map((c) => c.id));
  const people = withEmail.filter((c) => hasInsight(insights.get(c.id)));

  if (timeline.error) {
    return <ErrorState title="Couldn't load the emails" message={timeline.error.detail} onRetry={timeline.refetch} />;
  }
  if (!timeline.data) return <Skeleton width="100%" height={240} />;
  return (
    <div className={styles.stack}>
      {people.length > 0 && (
        <Card padding="lg">
          <h3 className={styles.title}>How they reply</h3>
          <ul className={styles.people}>
            {people.map((c) => (
              <li key={c.id} className={styles.person}>
                <span className={styles.name}>{c.full_name}</span>
                <InsightBadge insight={insights.get(c.id)} />
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card padding="lg">
        <ConversationTimeline
          clamp
          empty="No emails have been sent to anyone here from NEXUS yet."
          messages={timeline.data.map((e) => ({
            id: e.message_id,
            direction: e.direction,
            subject: e.subject,
            body: e.preview,
            at: e.at,
            who: e.contact_name || undefined,
            context: e.direction === "out"
              ? `${e.contact_name ? `To ${e.contact_name} · ` : ""}${e.campaign_name || "One-off email"}`
              : e.campaign_name || undefined,
            category: (e.category as ReplyCategory | null) ?? null,
          }))}
        />
      </Card>
    </div>
  );
}
