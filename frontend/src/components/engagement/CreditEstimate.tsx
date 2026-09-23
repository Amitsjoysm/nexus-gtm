import type { CampaignEstimate } from "@/lib/types";
import { CAPABILITY_LINE } from "./labels";
import styles from "./CreditEstimate.module.css";

/**
 * What a campaign can cost before it launches (spec §10, D18).
 *
 * The WORST case is the gate and is shown first: every contact receives every email and replies
 * once. The likely cost is information only. Prices come from the live rate cards through the same
 * code the meter charges with, so the number here is the number spent.
 */

function credits(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

export function CreditEstimate({ estimate }: { estimate: CampaignEstimate }) {
  const lines = Object.entries(estimate.per_capability).filter(([, l]) => l.units > 0);
  return (
    <section className={styles.estimate} aria-labelledby="estimate-title">
      <h3 id="estimate-title" className={styles.title}>What this campaign can cost</h3>
      <dl className={styles.figures}>
        <div className={styles.figure}>
          <dt>Most it can cost</dt>
          <dd className={styles.worst}>{credits(estimate.worst_credits)} credits</dd>
          <dd className={styles.note}>
            {estimate.contacts} {estimate.contacts === 1 ? "contact" : "contacts"} ×{" "}
            {estimate.email_steps} {estimate.email_steps === 1 ? "email" : "emails"}, each
            replying once
          </dd>
        </div>
        <div className={styles.figure}>
          <dt>Likely</dt>
          <dd className={styles.likely}>{credits(estimate.likely_credits)} credits</dd>
          <dd className={styles.note}>If 15% reply and follow-ups stop when they do</dd>
        </div>
        <div className={styles.figure}>
          <dt>Your balance</dt>
          <dd className={styles.likely}>{credits(estimate.balance)} credits</dd>
        </div>
      </dl>

      {!estimate.gate_applies ? (
        <p className={styles.status} role="status">
          Credits are not checked before launch on this workspace's plan.
        </p>
      ) : estimate.covered ? (
        <p className={`${styles.status} ${styles.ok}`} role="status">
          Your balance covers the most this campaign can cost.
        </p>
      ) : (
        <p className={`${styles.status} ${styles.short}`} role="alert">
          Short by {credits(estimate.shortfall)} credits. Launching waits until your balance covers
          the most it can cost, so no one is left half-way through a sequence.
        </p>
      )}

      {lines.length > 0 && (
        <table className={styles.table}>
          <caption className={styles.caption}>Where the most it can cost comes from</caption>
          <thead>
            <tr>
              <th scope="col">Action</th>
              <th scope="col" className={styles.num}>Times</th>
              <th scope="col" className={styles.num}>Credits</th>
            </tr>
          </thead>
          <tbody>
            {lines.map(([id, line]) => (
              <tr key={id}>
                <th scope="row">{CAPABILITY_LINE[id] ?? id}</th>
                <td className={styles.num}>{line.units.toLocaleString()}</td>
                <td className={styles.num}>{credits(line.credits)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
