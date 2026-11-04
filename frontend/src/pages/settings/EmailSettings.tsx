import { PageHeader } from "@/components/layout/PageHeader";
import { EmailStyleCard, MailboxesCard } from "@/pages/SettingsPage";
import styles from "@/pages/SettingsPage.module.css";

/**
 * Settings > Email defaults (admin; "Email & signature" until 2026-11-04): the workspace's shared
 * SMTP sending mailboxes, and the signature and writing style a mailbox uses until its owner sets
 * its own. Each rep's own Gmail or Outlook mailbox, and its own style, is Settings > My mailboxes.
 */
export function EmailSettingsPage() {
  return (
    <div>
      <PageHeader
        title="Email defaults"
        description="Shared sending mailboxes, and the signature and writing style a mailbox uses until its owner sets its own in My mailboxes."
      />
      <div className={styles.stack}>
        <MailboxesCard />
        <EmailStyleCard />
      </div>
    </div>
  );
}

export default EmailSettingsPage;
