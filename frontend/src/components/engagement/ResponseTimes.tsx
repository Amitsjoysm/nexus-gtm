import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ResponseTimeRow } from "@/lib/types";
import styles from "./ResponseTimes.module.css";

/**
 * How fast replies that needed an answer were answered, over the last 30 days (spec §11), in
 * business hours in each mailbox's own zone: the clock the reply-speed reminder runs on. A median
 * with the 90th percentile beside it, because one slow week hides inside an average.
 *
 * One line for yourself; a table when a manager is looking at the team.
 */

function hours(h: number | null): string {
  if (h === null) return "no answers yet";
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
  return `${Math.round(h * 10) / 10} h`;
}

export function ResponseTimes({ team }: { team: boolean }) {
  const api = useApiClient();
  const rows = useApi<ResponseTimeRow[]>((s) => api.responseTimes(team, s), [team]);
  if (rows.error || !rows.data || rows.data.length === 0) return null;

  if (!team) {
    const r = rows.data[0];
    if (r.answered === 0) {
      return (
        <p className={styles.line} aria-live="polite">
          Last 30 days: none answered yet
          {r.waiting > 0 && <>; {r.waiting} waiting for an answer</>}.
        </p>
      );
    }
    return (
      <p className={styles.line} aria-live="polite">
        Last 30 days: {r.answered} {r.answered === 1 ? "reply" : "replies"} answered, typically in{" "}
        <strong>{hours(r.median_hours)}</strong> of working time
        {r.p90_hours !== null && <> (9 in 10 within {hours(r.p90_hours)})</>}
        {r.waiting > 0 && <>; {r.waiting} still waiting</>}.
      </p>
    );
  }
  return (
    <table className={styles.table}>
      <caption className={styles.caption}>
        Time to first answer over the last 30 days, in working hours.
      </caption>
      <thead>
        <tr>
          <th scope="col">Person</th>
          <th scope="col" className={styles.num}>Answered</th>
          <th scope="col" className={styles.num}>Typical</th>
          <th scope="col" className={styles.num}>9 in 10 within</th>
          <th scope="col" className={styles.num}>Waiting</th>
        </tr>
      </thead>
      <tbody>
        {rows.data.map((r) => (
          <tr key={r.user_id}>
            <th scope="row">{r.name || "Unknown"}</th>
            <td className={styles.num}>{r.answered}</td>
            <td className={styles.num}>{hours(r.median_hours)}</td>
            <td className={styles.num}>{hours(r.p90_hours)}</td>
            <td className={styles.num}>{r.waiting}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
