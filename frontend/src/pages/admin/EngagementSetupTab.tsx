import { Badge, Button, CardHeader, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import type { EngagementSetup, MailboxAppSetup } from "@/lib/types";
import styles from "./EngagementSetupTab.module.css";

/**
 * What to paste into Google Cloud and Azure, and whether each piece is in place (spec §12).
 *
 * Connecting SDR mailboxes depends on two consoles the product does not control. Every value those
 * consoles ask for is derived on the server from the public base URL, so it is copied rather than
 * composed: a hand-typed redirect URI with one wrong character fails the OAuth flow with an error
 * that names neither the URI nor the fix.
 *
 * Secrets are never shown. They are entered in Provider keys, where each has a Test button; this
 * screen says only whether one is stored. The step-by-step guides live in `docs/engagement/`.
 */
export function EngagementSetupTab() {
  const api = useApiClient();
  const setup = useApi<EngagementSetup>((signal) => api.engagementSetup(signal), []);

  return (
    <div className={styles.wrap}>
      <CardHeader
        title="Mailbox apps"
        subtitle="The Google and Microsoft apps SDRs connect their mailboxes through, the notification endpoints replies arrive on, and the training ledger stores. Secrets go in Provider keys."
      />
      <DataState
        state={setup}
        errorTitle="Couldn't load the mailbox app setup"
        skeleton={<Skeleton width="100%" height={320} />}
      >
        {(s) => (
          <>
            <section className={styles.section} aria-labelledby="eng-base">
              <h3 id="eng-base" className={styles.heading}>Public base URL</h3>
              {s.public_base_url ? (
                <CopyRow label="Base URL" value={s.public_base_url} />
              ) : (
                <p className={styles.missing}>
                  Not set. Set it under Configuration → Mailboxes &amp; engagement; every URL below is
                  built from it.
                </p>
              )}
              <p className={styles.note}>
                Campaigns and reply desk:{" "}
                <Badge tone={s.campaigns_enabled ? "success" : "neutral"}>
                  {s.campaigns_enabled ? "On" : "Off"}
                </Badge>
              </p>
            </section>

            {s.mailbox_apps.map((app) => (
              <MailboxAppSection key={app.provider} app={app} />
            ))}

            <section className={styles.section} aria-labelledby="eng-gmail-push">
              <h3 id="eng-gmail-push" className={styles.heading}>Gmail reply notifications</h3>
              <CopyRow label="Push endpoint" value={s.gmail_push_audience} />
              <CopyRow label="OIDC audience" value={s.gmail_push_audience} />
              <ValueRow label="Topic" value={s.gmail_pubsub_topic} />
              <ValueRow label="Push service account" value={s.gmail_push_service_account} />
            </section>

            <section className={styles.section} aria-labelledby="eng-graph">
              <h3 id="eng-graph" className={styles.heading}>Microsoft reply notifications</h3>
              <CopyRow label="Notification URL" value={s.graph_notification_url} />
            </section>

            <section className={styles.section} aria-labelledby="eng-ledger">
              <h3 id="eng-ledger" className={styles.heading}>Training &amp; insights ledger</h3>
              <ul className={styles.checks}>
                {Object.entries(s.ledger_stores).map(([store, ok]) => (
                  <li key={store}>
                    <Badge tone={ok ? "success" : "warning"} dot>
                      {ok ? "Stored" : "Missing"}
                    </Badge>{" "}
                    {store} store connection (Provider keys → ledger_{store})
                  </li>
                ))}
                <li>
                  <Badge tone={s.pseudonym_secret_configured ? "success" : "warning"} dot>
                    {s.pseudonym_secret_configured ? "Stored" : "Missing"}
                  </Badge>{" "}
                  Pseudonymisation secret (Provider keys → ledger_pseudonym)
                </li>
              </ul>
            </section>
          </>
        )}
      </DataState>
    </div>
  );
}

function MailboxAppSection({ app }: { app: MailboxAppSetup }) {
  const title = app.provider === "google" ? "Google (Gmail)" : "Microsoft 365 (Outlook)";
  return (
    <section className={styles.section} aria-labelledby={`eng-${app.provider}`}>
      <h3 id={`eng-${app.provider}`} className={styles.heading}>
        {title}{" "}
        <Badge tone={app.configured ? "success" : "warning"}>
          {app.configured ? "Configured" : "Not configured"}
        </Badge>
      </h3>
      {app.missing.length > 0 && (
        <ul className={styles.missingList} aria-label={`${title}: still missing`}>
          {app.missing.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
      <CopyRow label="Redirect URI" value={app.redirect_uri} />
      <ValueRow label="Client id" value={app.client_id} />
      {app.provider === "microsoft" && <ValueRow label="Tenant" value={app.tenant} />}
      <CopyRow label="Scopes" value={app.scopes.join(" ")} />
    </section>
  );
}

function ValueRow({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.row}>
      <span className={styles.label}>{label}</span>
      <code className={styles.value}>{value || "—"}</code>
    </div>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  const toast = useToast();
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      toast.success("Copied", `${label} is on your clipboard.`);
    } catch {
      toast.error("Couldn't copy", "Select the value and copy it by hand.");
    }
  }
  return (
    <div className={styles.row}>
      <span className={styles.label}>{label}</span>
      <code className={styles.value}>{value || "—"}</code>
      <Button size="sm" variant="secondary" onClick={copy} disabled={!value}
        aria-label={`Copy ${label}`}>
        Copy
      </Button>
    </div>
  );
}
