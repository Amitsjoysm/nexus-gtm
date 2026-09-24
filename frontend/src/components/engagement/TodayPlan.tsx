import { Link } from "react-router-dom";
import { Card, CardHeader, Icons, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { TodayItem, TodayKind } from "@/lib/types";
import styles from "./TodayPlan.module.css";

/**
 * Today (spec §19, "Where do I start?"): one ordered list, built by the server, in the order of what
 * costs most if left. A buyer who said yes and is waiting comes first; people returning today come
 * last, because they need nothing.
 *
 * Renders nothing when there is nothing to do, and nothing when it cannot load: the dashboard has
 * plenty else to show, and an error card about an optional list is noise on every page load.
 */

const KIND: Record<TodayKind, { label: string; icon: JSX.Element }> = {
  reply: { label: "Answer", icon: <Icons.MessageIcon /> },
  decide: { label: "Decide", icon: <Icons.HelpCircleIcon /> },
  colleagues: { label: "Colleagues", icon: <Icons.UsersIcon /> },
  call: { label: "Call", icon: <Icons.PhoneIcon /> },
  review: { label: "Review", icon: <Icons.CheckIcon /> },
  restart: { label: "Write again", icon: <Icons.SignalIcon /> },
  returning: { label: "Returning", icon: <Icons.RefreshIcon /> },
};

export function TodayPlan() {
  const api = useApiClient();
  const plan = useApi<TodayItem[]>((s) => api.engagementToday(s), []);

  if (plan.error || (plan.data && plan.data.length === 0)) return null;
  return (
    <Card padding="md" className={styles.card}>
      <CardHeader
        title="Today"
        subtitle={plan.data ? `${plan.data.length} ${plan.data.length === 1 ? "thing" : "things"}, most urgent first` : undefined}
      />
      {!plan.data ? (
        <div className={styles.loading}>
          <Skeleton width="100%" height={44} />
          <Skeleton width="100%" height={44} />
        </div>
      ) : (
        <ol className={styles.list}>
          {plan.data.map((item, i) => {
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
