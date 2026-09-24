import { Badge } from "@/components/ui";
import type { ConnectedMailbox } from "@/lib/types";
import styles from "./MailboxHealth.module.css";

/**
 * This week's bounce rate for one mailbox, beside the over-50-a-day warning (spec §19). Status
 * colour never stands alone: the badge says the number, and a warning says what to do.
 */
export function MailboxHealth({ mailbox }: { mailbox: ConnectedMailbox }) {
  if (mailbox.sent_7d === 0) return null;
  const rate = `${Math.round(mailbox.bounce_rate_7d * 1000) / 10}%`;
  return (
    <div className={styles.health}>
      <p className={styles.line}>
        <Badge tone={mailbox.health_warning ? "warning" : "neutral"} dot>{rate} bounced</Badge>
        <span className={styles.muted}>
          {mailbox.bounced_7d} of {mailbox.sent_7d} sent in the last 7 days
        </span>
      </p>
      {mailbox.health_warning && <p className={styles.warning} role="status">{mailbox.health_warning}</p>}
    </div>
  );
}
