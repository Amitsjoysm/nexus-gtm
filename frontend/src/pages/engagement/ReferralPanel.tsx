import { useEffect, useState } from "react";
import { Button, ButtonLink, Field, Icons, Input, WorkingIndicator } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ReferralCandidate, ReferralResult } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Referral follow-through (spec §19): the person a reply points to, added to the same campaign
 * with an intro that says who suggested it, waiting in the review queue.
 *
 * The server reads who was named (the Cc line first, then the reply's own words) and says whether
 * each is already a contact. Nothing is spent until Draft intro: then an address left blank is
 * looked up (an enrichment credit) and the intro is drafted (a draft credit). Nothing is sent: the
 * intro is approved in the campaign like every opening email.
 */
export function ReferralPanel({ id }: { id: string }) {
  const api = useApiClient();
  const toast = useToast();
  const named = useApi<ReferralCandidate[]>((s) => api.deskReferralCandidates(id, s), [id]);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<ReferralResult | null>(null);

  useEffect(() => {
    const first = named.data?.[0];
    if (!first) return;
    setName(first.contact_name || first.name);
    setEmail(first.contact_email || first.email);
  }, [named.data]);

  async function draftIntro() {
    setBusy(true);
    try {
      const result = await api.deskReferral(id, { name: name.trim(), email: email.trim() });
      setDone(result);
      if (!result.drafted) {
        toast.error("Added, but the intro was not drafted", result.error || "Open the campaign to draft it again.");
      }
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  function startOver() {
    setDone(null);
    setName("");
    setEmail("");
  }

  return (
    <section className={styles.answer} aria-labelledby="referral-title">
      <h3 id="referral-title" className={styles.sectionTitle}>They pointed you to someone</h3>

      {done ? (
        <>
          <p className={styles.notice} role="status">
            {done.contact_name} is in {done.campaign_name}
            {done.drafted ? ", with an intro waiting for your review." : ". Their intro still needs drafting."}
          </p>
          <div className={styles.formActions}>
            <Button variant="secondary" iconLeft={<Icons.PlusIcon />} onClick={startOver}>
              Add someone else
            </Button>
            <ButtonLink to={`/engagement/campaigns/${done.campaign_id}?tab=review`} variant="primary">
              Review the intro
            </ButtonLink>
          </div>
        </>
      ) : (
        <>
          <p className={styles.muted}>
            Add them to the same campaign with an intro that says who suggested it. Nothing is sent
            until you approve it there.
          </p>

          {named.data && named.data.length > 0 && (
            <ul className={styles.candidates} aria-label="People named in the reply">
              {named.data.map((c) => {
                const shown = c.contact_name || c.name || c.email;
                const address = c.contact_email || c.email;
                const chosen = (c.contact_name || c.name) === name && (address || "") === email;
                return (
                  <li key={`${c.email}-${c.name}`}>
                    <button
                      type="button" className={styles.candidate} aria-pressed={chosen} disabled={busy}
                      onClick={() => { setName(c.contact_name || c.name); setEmail(address || ""); }}
                    >
                      <span className={styles.personName}>{shown}</span>
                      <span className={styles.muted}>
                        {[address, c.evidence, c.contact_id ? "already a contact" : ""].filter(Boolean).join(" · ")}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {named.data?.length === 0 && (
            <p className={styles.muted}>Nobody is named clearly enough to pick out. Type who they suggested.</p>
          )}

          {busy ? (
            <WorkingIndicator label="Finding them and writing the intro" hint="Looking up an address takes longer than a draft alone." />
          ) : (
            <div className={styles.fields}>
              <Field label="Name">
                <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
              </Field>
              <Field label="Email" hint="Leave it blank to look it up, which uses an enrichment credit.">
                <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
              </Field>
            </div>
          )}

          <div className={styles.formActions}>
            <Button iconLeft={<Icons.SparklesIcon />} onClick={draftIntro} loading={busy}
              disabled={busy || (!name.trim() && !email.trim())}>
              Draft intro
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
