import { Card, ErrorState, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ReplyCategory, TimelineEntry } from "@/lib/types";
import { ConversationTimeline } from "./ConversationTimeline";

/**
 * Every email to and from the people at an account (or one contact), across campaigns and one-off
 * sends (spec §9, "a cross-campaign conversation timeline"). Previews, oldest first; the full thread
 * of a reply is on the reply desk.
 */
export function AccountConversations({ accountId, contactId }: { accountId?: string; contactId?: string }) {
  const api = useApiClient();
  const timeline = useApi<TimelineEntry[]>(
    (s) => api.engagementTimeline({ account_id: accountId, contact_id: contactId }, s),
    [accountId, contactId],
  );

  if (timeline.error) {
    return <ErrorState title="Couldn't load the emails" message={timeline.error.detail} onRetry={timeline.refetch} />;
  }
  if (!timeline.data) return <Skeleton width="100%" height={240} />;
  return (
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
  );
}
