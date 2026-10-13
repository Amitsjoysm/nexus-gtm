import { PageHeader } from "@/components/layout/PageHeader";
import { EmailStyleCard, MailboxesCard } from "@/pages/SettingsPage";
import styles from "@/pages/SettingsPage.module.css";

/**
 * Settings > Email & signature (admin): the workspace's shared SMTP sending mailboxes and how
 * drafts read and sign off. Each rep's own Gmail or Outlook mailbox is Settings > My mailboxes.
 */
export function EmailSettingsPage() {
  return (
    <div>
      <PageHeader
        title="Email & signature"
        description="Shared sending mailboxes, and how drafts read and sign off when a rep has not set their own."
      />
      <div className={styles.stack}>
        <MailboxesCard />
        <EmailStyleCard />
      </div>
    </div>
  );
}

export default EmailSettingsPage;
