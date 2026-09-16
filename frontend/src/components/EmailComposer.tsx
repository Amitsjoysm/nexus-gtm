import { useEffect, useState } from "react";
import { Badge, Button, Icons, useToast, WorkingIndicator } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import styles from "./EmailComposer.module.css";

interface EmailComposerProps {
  accountId: string;
  contactId: string;
  contactName: string;
  contactEmail?: string | null;
  /** The verifier's verdict, so the rep knows what they are sending into before they send it. */
  emailStatus?: string | null;
}

/**
 * Draft a hyper-personalized first email and send it — in one click.
 *
 * The composer used to end at Copy and a `mailto:` link that dumps the draft into Outlook, so a
 * rep's last step was copy-and-paste. Everything needed to actually send already existed and was
 * wired to nothing: per-mailbox SMTP in Settings, a real sender, and a priced `outreach.email_send`
 * capability. See `nexus/outreach/send.py`.
 *
 * **The rep's click is the review.** They read it, they edited it, they pressed Send. The
 * "nothing sends until you approve it" rule governs automated outreach, which is still gated.
 *
 * The address status is shown BEFORE the send rather than reported after it. A rep about to email
 * an unverified address should know that while they can still decide not to.
 */

/**
 * What actually went wrong, in the rep's terms.
 *
 * A 402 from a superadmin FEATURE SWITCH and a 402 from a plan limit are the same status code and
 * completely different situations, and the server distinguishes them: `switch_state` and
 * `switch_message` ride on the payload for exactly this reason. Rendering `res.statusText` turned a
 * feature WE turned off into "Payment Required" on a workspace with an unlimited plan — observed
 * live 2026-09-09 with `module.outreach` disabled.
 */
function explainFailure(err: unknown): string {
  if (!(err instanceof ApiError)) return "Couldn't generate the email.";
  if (err.switchState && err.switchState !== "enabled") {
    // The operator's own words win when they left any — that is the whole point of the message
    // field on a switch.
    if (err.switchMessage.trim()) return err.switchMessage.trim();
    const label: Record<string, string> = {
      disabled: "Email drafting has been turned off for all workspaces by your administrator.",
      coming_soon: "Email drafting is not available yet.",
      maintenance: "Email drafting is temporarily down for maintenance. Try again shortly.",
    };
    return label[err.switchState] ?? "Email drafting is unavailable right now.";
  }
  if (err.status === 402) {
    return `${err.detail} — this workspace is over its plan limit for drafting.`;
  }
  return err.detail || "Couldn't generate the email.";
}

/** How a verifier verdict reads to a rep, and how alarmed to be about it. */
function statusChip(
  status: string,
): { tone: "success" | "warning" | "danger" | "info"; text: string } | null {
  const s = (status || "").toLowerCase();
  if (s === "valid") return { tone: "success", text: "Address verified" };
  if (s === "invalid") return { tone: "danger", text: "Address looks invalid" };
  // Not a warning: nothing is wrong with the address, the domain just accepts every recipient, so
  // no verifier can confirm it. Say which of those two situations the rep is in.
  if (s === "catch_all") return { tone: "info", text: "Catch-all domain — can't be confirmed" };
  if (s === "risky") return { tone: "warning", text: "Address is risky" };
  if (s === "unknown") return { tone: "warning", text: "Address unverified" };
  return null;
}

export function EmailComposer({
  accountId,
  contactId,
  contactName,
  contactEmail,
  emailStatus,
}: EmailComposerProps) {
  const api = useApiClient();
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sentTo, setSentTo] = useState<string | null>(null);
  /** Saving to Drafts is its own action with its own spinner: it is not a send in progress. */
  const [saving, setSaving] = useState(false);
  const [draftedIn, setDraftedIn] = useState<string | null>(null);
  /** Set when the server refused an invalid address; the retry carries the acknowledgement. */
  const [needsRiskyConfirm, setNeedsRiskyConfirm] = useState(false);
  /** When the draft on screen was written — shown so a reused one is visibly today's. */
  const [draftedAt, setDraftedAt] = useState<string | null>(null);

  async function generate() {
    setLoading(true);
    setError(null);
    setSentTo(null);
    setDraftedIn(null);
    setNeedsRiskyConfirm(false);
    setDraftedAt(null);
    try {
      const res = await api.runAgent("messaging", accountId, { contact_id: contactId });
      const out = res.output ?? {};
      // The agent reports a blank completion as an error rather than an empty draft — show its
      // reason, which says whether the failure is transient and where to look if not.
      if (typeof out.error === "string" && out.error) {
        setSubject("");
        setBody("");
        setError(typeof out.detail === "string" ? out.detail : "The writer returned nothing.");
        return;
      }
      setSubject(typeof out.subject === "string" ? out.subject : "");
      setBody(typeof out.body === "string" ? out.body : "");
      setDraftedAt(new Date().toISOString());
    } catch (err) {
      setError(explainFailure(err));
    } finally {
      setLoading(false);
    }
  }

  /**
   * Opening the composer used to write — and bill — a brand-new draft every time. The draft was
   * saved on the server all along, so today's is reused. ONLY today's: it proposes specific
   * meeting dates, and one from yesterday could one-click-send days that have passed. Anything
   * else (none saved, stale, a failed write, a lookup that errored) falls through to writing one.
   */
  async function openDraft() {
    setLoading(true);
    setError(null);
    setSentTo(null);
    setDraftedIn(null);
    setNeedsRiskyConfirm(false);
    try {
      const saved = (await api.latestAgentRuns(accountId, contactId)).messaging;
      const out = saved?.output ?? {};
      if (
        saved?.fresh &&
        !out.error &&
        typeof out.subject === "string" &&
        typeof out.body === "string" &&
        out.body.trim()
      ) {
        setSubject(out.subject);
        setBody(out.body);
        setDraftedAt(saved.created_at);
        setLoading(false);
        return;
      }
    } catch {
      /* a lookup we could not do must not stop a draft being written */
    }
    await generate();
  }

  useEffect(() => {
    void openDraft();
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

  async function send(allowRisky = false) {
    setSending(true);
    try {
      const res = await api.sendEmailToContact(contactId, { subject, body, allow_risky: allowRisky });
      if (res.ok) {
        setSentTo(res.to);
        setDraftedIn(null);  // it has left; "saved in Drafts" would be the wrong thing to still say
        setNeedsRiskyConfirm(false);
        toast.success(`Sent to ${res.to}`, `From ${res.from_email}.`);
      } else {
        // A 200 with ok:false carries the SMTP server's own reason — "authentication failed" and
        // "recipient rejected" send a rep to two completely different places.
        toast.error("The mail server refused it", res.detail);
      }
    } catch (err) {
      const detail = err instanceof ApiError ? explainFailure(err) : "Couldn't send.";
      if (err instanceof ApiError && err.status === 422 && /invalid/i.test(detail)) {
        // The address guard. Offer the override rather than dead-ending: the rep may know the
        // address is good, and the verifier returns `unknown` far more often than anything else.
        setNeedsRiskyConfirm(true);
      }
      toast.error("Not sent", detail);
    } finally {
      setSending(false);
    }
  }

  /**
   * Keep it instead of sending it: the same mailbox, the same signature, written into the rep's own
   * Drafts folder over IMAP so the last read happens in the mail client they live in.
   *
   * Never charged — `outreach.email_send` prices a message that left the building.
   */
  async function saveDraft() {
    setSaving(true);
    try {
      const res = await api.saveEmailDraft(contactId, { subject, body });
      if (res.ok) {
        setDraftedIn(res.from_email || "your mailbox");
        toast.success("Saved to Drafts", `In ${res.from_email}. Send it from your mail client.`);
      } else {
        // The IMAP server's own words: "authentication failed" and "no such folder" send a rep to
        // two different places.
        toast.error("The mail server refused it", res.detail);
      }
    } catch (err) {
      toast.error(
        "Not saved",
        err instanceof ApiError ? explainFailure(err) : "Couldn't save the draft.",
      );
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className={styles.loading}>
        {/* Measured over the last 30 days of drafts: p90 5s, slowest 13s. */}
        <WorkingIndicator
          label={`Writing a personalized email for ${contactName}`}
          hint="Usually under 15 seconds."
          slowAfter={20}
        />
      </div>
    );
  }

  const chip = statusChip(emailStatus || "");
  const busy = sending || saving;
  const canSend = Boolean(contactEmail) && body.trim().length > 0 && !busy;

  return (
    <div className={styles.composer}>
      {error && <div className={styles.error}>{error}</div>}

      {sentTo ? (
        <div className={styles.sent} role="status">
          <Icons.CheckIcon aria-hidden />
          <span>
            Sent to <strong>{sentTo}</strong>. Regenerate to write another.
          </span>
        </div>
      ) : draftedIn ? (
        <div className={styles.sent} role="status">
          <Icons.CheckIcon aria-hidden />
          <span>
            Saved in the Drafts folder of <strong>{draftedIn}</strong>. Nothing has been sent.
          </span>
        </div>
      ) : (
        contactEmail && (
          <div className={styles.recipient}>
            <span className={styles.to}>
              To <strong>{contactEmail}</strong>
            </span>
            {chip && (
              <Badge tone={chip.tone} dot>
                {chip.text}
              </Badge>
            )}
          </div>
        )
      )}

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

      {/* The draft deliberately ends at the sign-off: the signature block is appended from the
          sending mailbox (Settings → Sending mailboxes), so one rep's draft cannot go out under
          another's details, and regenerating never loses it. */}
      <p className={styles.hint}>
        Your signature is added from your sending mailbox when this goes out.
      </p>

      {needsRiskyConfirm && (
        <p className={styles.warn} role="alert">
          That address was verified as invalid, so it will almost certainly bounce — and bounces
          cost your domain its ability to deliver anything. Send anyway only if you know it is good.
        </p>
      )}

      <div className={styles.actions}>
        {draftedAt && (
          <span className={styles.when}>
            Drafted{" "}
            {new Date(draftedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}{" "}
            today
          </span>
        )}
        <Button variant="secondary" iconLeft={<Icons.RefreshIcon />} onClick={generate}>
          Regenerate
        </Button>
        <Button variant="secondary" onClick={copy}>
          Copy
        </Button>
        {/* The other half of the choice: keep it for a last read in the mail client the rep lives
            in. Same mailbox, same signature, nothing sent and nothing charged. Refused with a
            reason when the mailbox has no IMAP host (Settings → Sending mailboxes). */}
        <Button
          variant="secondary"
          loading={saving}
          disabled={!canSend}
          onClick={saveDraft}
        >
          Save to Drafts
        </Button>
        {needsRiskyConfirm ? (
          <Button variant="danger" loading={sending} disabled={saving} onClick={() => send(true)}>
            Send anyway
          </Button>
        ) : (
          <Button
            iconLeft={<Icons.SendIcon />}
            loading={sending}
            disabled={!canSend}
            onClick={() => send(false)}
          >
            {contactEmail ? "Send email" : "No address on file"}
          </Button>
        )}
      </div>
    </div>
  );
}
