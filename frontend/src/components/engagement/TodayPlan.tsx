import { Link } from "react-router-dom";
import { ButtonLink, Card, CardHeader, Icons, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { TodayItem, TodayKind, TodaySection } from "@/lib/types";
import styles from "./TodayPlan.module.css";

/**
 * Today on the Dashboard (spec §19, "Where do I start?"): how much is waiting in each Inbox section,
 * the three most urgent things, and the way into the Inbox, which is where the day is worked (SDR
 * round 2, 6.1: the Inbox is the one task list, so this card summarises it rather than repeating
 * it). The order is the server's: what costs most if left comes first.
 *
 * Renders nothing when there is nothing to do, and nothing when it cannot load: the dashboard has
 * plenty else to show, and an error card about an optional list is noise on every page load.
 */

const KIND: Record<TodayKind, { label: string; icon: JSX.Element }> = {
  reply: { label: "Answer", icon: <Icons.MessageIcon /> },
  decide: { label: "Decide", icon: <Icons.HelpCircleIcon /> },
  colleagues: { label: "Colleagues", icon: <Icons.UsersIcon /> },
  call: { label: "Call", icon: <Icons.PhoneIcon /> },
  followup: { label: "Follow-up", icon: <Icons.MailIcon /> },
  review: { label: "Review", icon: <Icons.CheckIcon /> },
  restart: { label: "Write again", icon: <Icons.SignalIcon /> },
  returning: { label: "Returning", icon: <Icons.RefreshIcon /> },
};

/** Each section's count as words, [one, many]. */
const SECTION: Record<TodaySection, [string, string]> = {
  calls: ["call", "calls"], emails: ["email", "emails"], replies: ["reply", "replies"],
};

/** How many items the card shows before pointing at the Inbox for the rest. */
const PREVIEW = 3;

export function TodayPlan() {
  const api = useApiClient();
  const plan = useApi<TodayItem[]>((s) => api.engagementToday(s), []);

  if (plan.error || (plan.data && plan.data.length === 0)) return null;
  const items = plan.data ?? [];
  const overdue = items.filter((i) => i.due === "overdue").length;
  const counts = (Object.keys(SECTION) as TodaySection[])
    .map((s) => [s, items.filter((i) => i.section === s).length] as const)
    .filter(([, n]) => n > 0)
    .map(([s, n]) => `${n} ${SECTION[s][n === 1 ? 0 : 1]}`);
  return (
    <Card padding="md" className={styles.card}>
      <CardHeader
        title="Today"
        subtitle={plan.data
          ? `${counts.join(" · ")}${overdue ? `, ${overdue} overdue` : ""}`
          : undefined}
        actions={<ButtonLink to="/inbox" size="sm" iconLeft={<Icons.InboxIcon />}>Open Inbox</ButtonLink>}
      />
      {!plan.data ? (
        <div className={styles.loading}>
          <Skeleton width="100%" height={44} />
          <Skeleton width="100%" height={44} />
        </div>
      ) : (
        <ol className={styles.list}>
          {items.slice(0, PREVIEW).map((item, i) => {
            const kind = KIND[item.kind];
            return (
              <li key={`${item.kind}-${i}`}>
                <Link to={item.link} className={styles.item}>
                  <span className={styles.icon} aria-hidden="true">{kind.icon}</span>
                  <span className={styles.text}>
                    <span className={styles.title}>{item.title}</span>
                    <span className={styles.detail}>{item.detail}</span>
                  </span>
                  <span className={styles.kind}>{kind.label}</span>
                </Link>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}
