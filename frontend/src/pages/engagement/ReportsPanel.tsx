import type { CSSProperties } from "react";
import { EmptyState, ErrorState, Icons, Skeleton } from "@/components/ui";
import { CATEGORY } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { CampaignReport, ReplyCategory } from "@/lib/types";
import styles from "./ReportsPanel.module.css";

/**
 * What a campaign achieved (spec §11), counted in people, not messages.
 *
 * The funnel is one series, so one hue and no legend: every bar carries its own count and its
 * share of the people sent to. Bounces are a problem, not a stage, so they sit beside the funnel
 * with a status icon rather than as a bar in it. Per step, a reply belongs to the last email the
 * person got before replying, so a follow-up that earned a reply gets the credit. A table under
 * each chart carries the same numbers for anyone who reads tables rather than bars.
 */

function pct(n: number): string {
  return `${Math.round(n * 1000) / 10}%`;
}

export function ReportsPanel({ campaignId }: { campaignId: string }) {
  const api = useApiClient();
  const report = useApi<CampaignReport>((s) => api.campaignReport(campaignId, s), [campaignId]);

  if (report.error) {
    return <ErrorState title="Couldn't load the results" message={report.error.detail} onRetry={report.refetch} />;
  }
  if (!report.data) return <Skeleton width="100%" height={320} />;
  const r = report.data;
  if (r.sent === 0) {
    return (
      <EmptyState icon={<Icons.TrendUpIcon />} title="Nothing sent yet"
        description="Results appear once the first emails go out: who replied, to which step, and what they said." />
    );
  }

  const funnel: [string, number][] = [
    ["People", r.contacts], ["Sent to", r.sent], ["Replied", r.replied],
    ["Positive", r.positive], ["Meetings", r.meetings],
  ];
  const widest = Math.max(1, ...funnel.map(([, n]) => n));
  const categories = Object.entries(r.categories);
  const mostCategory = Math.max(1, ...categories.map(([, n]) => n));
  const emailSteps = r.steps.filter((s) => s.channel === "email");

  return (
    <div className={styles.panel}>
      <section aria-labelledby="funnel-title" className={styles.block}>
        <h3 id="funnel-title" className={styles.title}>People, from added to meeting</h3>
        <ol className={styles.bars}>
          {funnel.map(([label, n], i) => (
            <li key={label} className={styles.barRow}>
              <span className={styles.label}>{label}</span>
              <span className={styles.track} aria-hidden="true">
                <span className={styles.bar} style={{ "--w": `${(n / widest) * 100}%` } as CSSProperties}
                  title={i > 0 ? `${n} of ${r.sent} sent to (${pct(n / (r.sent || 1))})` : `${n} people`} />
              </span>
              <span className={styles.value}>
                {n.toLocaleString()}
                {i > 1 && <span className={styles.rate}> · {pct(n / r.sent)}</span>}
              </span>
            </li>
          ))}
        </ol>
        {r.bounced > 0 && (
          <p className={styles.bounced} role="status">
            <Icons.AlertTriangleIcon aria-hidden className={styles.statusIcon} />
            {r.bounced} {r.bounced === 1 ? "address" : "addresses"} bounced ({pct(r.bounced / r.sent)} of those sent to).
            Bounces are stopped and added to the do-not-contact list.
          </p>
        )}
      </section>

      {emailSteps.length > 0 && (
        <section aria-labelledby="steps-title" className={styles.block}>
          <h3 id="steps-title" className={styles.title}>Reply rate by email</h3>
          <table className={styles.table}>
            <caption className={styles.caption}>
              A reply counts for the last email the person received before replying.
            </caption>
            <thead>
              <tr>
                <th scope="col">Email</th>
                <th scope="col" className={styles.num}>Sent</th>
                <th scope="col" className={styles.num}>Replies</th>
                <th scope="col" className={styles.num}>Reply rate</th>
              </tr>
            </thead>
            <tbody>
              {emailSteps.map((s, i) => (
                <tr key={s.step_index}>
                  <th scope="row">{i === 0 ? "Opening email" : `Follow-up ${i}`}</th>
                  <td className={styles.num}>{s.sent.toLocaleString()}</td>
                  <td className={styles.num}>{s.replies.toLocaleString()}</td>
                  <td className={styles.num}>{s.sent ? pct(s.reply_rate) : "Not sent yet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {categories.length > 0 && (
        <section aria-labelledby="categories-title" className={styles.block}>
          <h3 id="categories-title" className={styles.title}>What the replies said</h3>
          <ol className={styles.bars}>
            {categories.map(([category, n]) => (
              <li key={category} className={styles.barRow}>
                <span className={styles.label}>{CATEGORY[category as ReplyCategory]?.label ?? category}</span>
                <span className={styles.track} aria-hidden="true">
                  <span className={styles.bar} style={{ "--w": `${(n / mostCategory) * 100}%` } as CSSProperties}
                    title={`${n} ${n === 1 ? "reply" : "replies"}`} />
                </span>
                <span className={styles.value}>{n.toLocaleString()}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}
