import { useEffect, useState } from "react";
import { Button, Icons, Spinner, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import styles from "./EmailComposer.module.css";

interface EmailComposerProps {
  accountId: string;
  contactId: string;
  contactName: string;
  contactEmail?: string | null;
}

/**
 * Generates a hyper-personalized first email for a contact by running the messaging agent
 * (grounded in the account's signals, firmographics, and the person's brief). Subject and body
 * are editable; the rep can regenerate, copy, or open it in their mail client.
 */
export function EmailComposer({ accountId, contactId, contactName, contactEmail }: EmailComposerProps) {
  const api = useApiClient();
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function generate() {
    setLoading(true);
    setError(null);
    try {
      const res = await api.runAgent("messaging", accountId, { contact_id: contactId });
      const out = res.output ?? {};
      const s = typeof out.subject === "string" ? out.subject : "";
      const b = typeof out.body === "string" ? out.body : "";
      setSubject(s);
      setBody(b);
      if (!s && !b) {
        setError("The writer returned nothing — try Regenerate, or check that LLM keys are set.");
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Couldn't generate the email.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    generate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contactId]);

  async function copy() {
    const text = subject ? `Subject: ${subject}\n\n${body}` : body;
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied", "The email is on your clipboard.");
    } catch {
      toast.error("Couldn't copy", "Select the text and copy manually.");
    }
  }

  function openInMail() {
    if (!contactEmail) return;
    const href = `mailto:${contactEmail}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    window.location.href = href;
  }

  if (loading) {
    return (
      <div className={styles.loading}>
        <Spinner size={18} /> Writing a personalized email for {contactName}…
      </div>
    );
  }

  return (
    <div className={styles.composer}>
      {error && <div className={styles.error}>{error}</div>}
      <label className={styles.label} htmlFor="email-subject">
        Subject
      </label>
      <input
        id="email-subject"
        className={styles.subject}
        value={subject}
        onChange={(e) => setSubject(e.target.value)}
      />
      <label className={styles.label} htmlFor="email-body">
        Body
      </label>
      <textarea
        id="email-body"
        className={styles.body}
        rows={12}
        value={body}
        onChange={(e) => setBody(e.target.value)}
      />
      <div className={styles.actions}>
        <Button variant="secondary" iconLeft={<Icons.RefreshIcon />} onClick={generate}>
          Regenerate
        </Button>
        <Button variant="secondary" onClick={copy}>
          Copy
        </Button>
        {contactEmail && (
          <Button iconLeft={<Icons.SendIcon />} onClick={openInMail}>
            Open in email
          </Button>
        )}
      </div>
    </div>
  );
}
