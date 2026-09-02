import { useMemo, useState } from "react";
import { Badge, Card, CardHeader, EmptyState, Icons, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import type { CreditUsageReport as Report } from "@/lib/types";
import styles from "./CreditUsageReport.module.css";

/**
 * "Where did my credits go?"
 *
 * `/billing/usage` answered a different question: it reported per-capability ACTION COUNTS against
 * a quota. Under credits-only billing the quota is not what gates a priced action — the balance is
 * — so a workspace out of credits saw green meters beside a product that had stopped working.
 * This reports the thing that is actually being spent.
 *
 * Three views, because they answer three different questions, and each is the fastest route to a
 * different conclusion:
 *
 *   * by capability — "what is eating my balance?" Sorted by spend, since the top two rows are
 *     nearly always the whole answer.
 *   * by day — "why did it drop on Tuesday?" A bulk import and steady use produce the same total
 *     and completely different shapes.
 *   * by user — "who is spending it?"
 *
 * No charting library: the curated dependency list is react, react-dom, react-router-dom and
 * framer-motion. Both visuals are proportional bars built from the same track/fill vocabulary the
 * plan meters already use, which keeps one bar language across the billing surface rather than
 * introducing a second one that happens to be drawn by a library.
 */

/** `YYYY-MM-DD` to a short axis label, without constructing a Date per render pass. */
function dayLabel(iso: string): string {
  const parts = iso.split("-");
  if (parts.length !== 3) return iso;
  const d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Credits are fractional (verify.email is 0.25). Whole numbers stay clean; fractions keep 2dp. */
function credits(n: number): string {
  return Number.isInteger(n) ? formatNumber(n) : n.toFixed(2);
}

function Totals({ data }: { data: Report }) {
  // Deliberately NOT a hero-metric row: three plain figures with the relationship between them
  // stated. `granted - spent === balance` does not hold — those are period figures and the balance
  // can carry over — so showing them as one arithmetic strip would imply a sum that is wrong.
  return (
    <dl className={styles.totals}>
      <div className={styles.total}>
        <dt className={styles.totalLabel}>Granted this period</dt>
        <dd className={styles.totalValue}>{credits(data.granted)}</dd>
      </div>
      <div className={styles.total}>
        <dt className={styles.totalLabel}>Spent this period</dt>
        <dd className={cn(styles.totalValue, styles.totalSpend)}>{credits(data.spent)}</dd>
      </div>
      <div className={styles.total}>
        <dt className={styles.totalLabel}>Balance now</dt>
        <dd className={styles.totalValue}>
          {credits(data.balance)}
          {data.balance <= 0 && (
            <Badge tone="danger" dot className={styles.totalBadge}>
              empty
            </Badge>
          )}
        </dd>
      </div>
    </dl>
  );
}

function ByCapability({ rows, spent }: { rows: Report["by_capability"]; spent: number }) {
  const [expanded, setExpanded] = useState(false);
  // Six rows covers the long tail in practice while keeping the card scannable. The rest are one
  // click away rather than hidden, because "what else?" is a fair question about your own bill.
  const visible = expanded ? rows : rows.slice(0, 6);
  const rest = rows.length - visible.length;
  const max = rows[0]?.credits ?? 0;

  return (
    <>
      <ul className={styles.rows}>
        {visible.map((row) => {
          // Bar length is share of the LARGEST row, not of the total: against the total, a
          // realistic spread (one capability at 70%) squashes everything else into a stub and the
          // comparison the chart exists for disappears. The percentage-of-spend is stated in text
          // beside it, so the precise number is never inferred from bar length.
          const rel = max > 0 ? (row.credits / max) * 100 : 0;
          const share = spent > 0 ? (row.credits / spent) * 100 : 0;
          return (
            <li key={row.capability_id} className={styles.row}>
              <div className={styles.rowHead}>
                <span className={styles.rowName} title={row.capability_id}>
                  {row.name}
                </span>
                <span className={styles.rowFigures}>
                  <span className={styles.rowCredits}>{credits(row.credits)}</span>
                  <span className={styles.rowUnit}>credits</span>
                </span>
              </div>
              <div
                className={styles.track}
                role="img"
                aria-label={`${row.name}: ${credits(row.credits)} credits, ${share.toFixed(
                  0,
                )}% of spend, across ${formatNumber(row.actions)} ${
                  row.actions === 1 ? "action" : "actions"
                }`}
              >
                <div className={styles.fill} style={{ inlineSize: `${rel}%` }} />
              </div>
              <p className={styles.rowMeta}>
                {formatNumber(row.actions)} {row.actions === 1 ? "action" : "actions"}
                <span className={styles.rowDot} aria-hidden="true">
                  ·
                </span>
                {share.toFixed(0)}% of spend
              </p>
            </li>
          );
        })}
      </ul>
      {rest > 0 && (
        <button type="button" className={styles.more} onClick={() => setExpanded(true)}>
          Show {rest} more {rest === 1 ? "capability" : "capabilities"}
        </button>
      )}
      {expanded && rows.length > 6 && (
        <button type="button" className={styles.more} onClick={() => setExpanded(false)}>
          Show fewer
        </button>
      )}
    </>
  );
}

function ByDay({ days }: { days: Report["by_day"] }) {
  const max = useMemo(() => Math.max(...days.map((d) => d.credits), 0), [days]);
  // A column per day, so a 31-day month stays readable without horizontal scrolling. Labels are
  // thinned rather than rotated: rotated axis text is the first thing to become unreadable at this
  // width, and every column keeps its full figure in the accessible name regardless.
  const labelEvery = days.length > 16 ? Math.ceil(days.length / 8) : days.length > 8 ? 2 : 1;

  return (
    <div className={styles.chart}>
      <ol className={styles.bars}>
        {days.map((day, i) => {
          const pct = max > 0 ? (day.credits / max) * 100 : 0;
          return (
            <li key={day.date} className={styles.barCol}>
              <div className={styles.barTrack}>
                <div
                  className={styles.bar}
                  // A day with real spend never renders as nothing: a 2px floor keeps a small
                  // value visible as "a little" instead of reading as "none at all".
                  style={{ blockSize: day.credits > 0 ? `max(2px, ${pct}%)` : "0" }}
                  title={`${dayLabel(day.date)}: ${credits(day.credits)} credits`}
                />
              </div>
              <span className={cn(styles.barLabel, i % labelEvery !== 0 && styles.barLabelHidden)}>
                {dayLabel(day.date)}
              </span>
            </li>
          );
        })}
      </ol>
      {/* The bars are decorative once this table exists; screen readers and keyboard users get the
          real figures here rather than a pile of unlabelled divs. */}
      <table className={styles.srTable}>
        <caption>Credits spent per day</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Credits</th>
          </tr>
        </thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.date}>
              <th scope="row">{dayLabel(day.date)}</th>
              <td>{credits(day.credits)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ByUser({ data }: { data: Report }) {
  const max = Math.max(
    ...data.by_user.map((u) => u.credits),
    data.unattributed_credits,
    0,
  );

  return (
    <ul className={styles.rows}>
      {data.by_user.map((user) => {
        const rel = max > 0 ? (user.credits / max) * 100 : 0;
        return (
          <li key={user.user_id} className={styles.row}>
            <div className={styles.rowHead}>
              <span className={styles.rowName}>{user.email || user.user_id}</span>
              <span className={styles.rowFigures}>
                <span className={styles.rowCredits}>{credits(user.credits)}</span>
                <span className={styles.rowUnit}>credits</span>
              </span>
            </div>
            <div
              className={styles.track}
              role="img"
              aria-label={`${user.email || user.user_id}: ${credits(user.credits)} credits`}
            >
              <div className={styles.fill} style={{ inlineSize: `${rel}%` }} />
            </div>
          </li>
        );
      })}

      {data.unattributed_credits > 0 && (
        <li className={cn(styles.row, styles.rowSystem)}>
          <div className={styles.rowHead}>
            <span className={styles.rowName}>Automated work</span>
            <span className={styles.rowFigures}>
              <span className={styles.rowCredits}>{credits(data.unattributed_credits)}</span>
              <span className={styles.rowUnit}>credits</span>
            </span>
          </div>
          <div
            className={styles.track}
            role="img"
            aria-label={`Automated work: ${credits(data.unattributed_credits)} credits`}
          >
            <div
              className={cn(styles.fill, styles.fillSystem)}
              style={{
                inlineSize: `${max > 0 ? (data.unattributed_credits / max) * 100 : 0}%`,
              }}
            />
          </div>
          {/* ATTRIBUTION IS PARTIAL BY CONSTRUCTION — only usage events carry a user id, and a
              refresh sweep has nobody to attribute to. Naming the remainder is what lets these
              rows add up to the total; dropping it would leave the customer to notice the gap
              themselves against a balance they can check. */}
          <p className={styles.rowMeta}>
            Account refreshes, signal crawls and plays. No person triggered these.
          </p>
        </li>
      )}
    </ul>
  );
}

export function CreditUsageReport() {
  const api = useApiClient();
  const report = useApi<Report>((signal) => api.billingCreditUsage(signal), []);

  return (
    <Card padding="lg">
      <CardHeader
        title="Where your credits went"
        subtitle="This billing period, by capability, by day, and by person."
      />
      <DataState
        state={report}
        errorTitle="Couldn't load your credit usage"
        skeleton={
          <div className={styles.stack}>
            <Skeleton width="100%" height={64} />
            <Skeleton width="100%" height={140} />
            <Skeleton width="100%" height={120} />
          </div>
        }
        isEmpty={(d) => d.spent <= 0}
        empty={
          <>
            {report.data && <Totals data={report.data} />}
            <EmptyState
              icon={<Icons.BoltIcon />}
              title="No credits spent yet"
              description="Enrich an account, draft a message or run an agent, and every charge shows up here with what it was for."
            />
          </>
        }
      >
        {(data) => (
          <div className={styles.stack}>
            <Totals data={data} />

            <section className={styles.section} aria-labelledby="credits-by-capability">
              <h3 id="credits-by-capability" className={styles.sectionTitle}>
                What used them
              </h3>
              <ByCapability rows={data.by_capability} spent={data.spent} />
            </section>

            {data.by_day.length > 1 && (
              <section className={styles.section} aria-labelledby="credits-by-day">
                <h3 id="credits-by-day" className={styles.sectionTitle}>
                  Day by day
                </h3>
                <ByDay days={data.by_day} />
              </section>
            )}

            {(data.by_user.length > 0 || data.unattributed_credits > 0) && (
              <section className={styles.section} aria-labelledby="credits-by-user">
                <h3 id="credits-by-user" className={styles.sectionTitle}>
                  Who used them
                </h3>
                <ByUser data={data} />
              </section>
            )}
          </div>
        )}
      </DataState>
    </Card>
  );
}

export default CreditUsageReport;
