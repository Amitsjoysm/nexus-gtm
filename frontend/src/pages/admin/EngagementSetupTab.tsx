import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Badge, Button, CardHeader, Field, Input, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import type { EngagementSetup, EngagementSetupInput, MailboxAppSetup } from "@/lib/types";
import styles from "./EngagementSetupTab.module.css";

/**
 * Set up the Google and Microsoft apps SDRs connect their mailboxes through (spec §12), in one place.
 *
 * It used to be read-only and send the operator to two other tabs: the ids to Configuration and
 * the secrets to Provider keys. The operator who reported it could not find Configuration at all.
 * Now every value is entered here and saved through the same server-side validators.
 *
 * Every value the consoles ask for is derived on the server from the public base URL, so it is
 * copied rather than composed: a hand-typed redirect URI with one wrong character fails the OAuth
 * flow with an error that names neither the URI nor the fix. Secrets are write-only: the screen
 * shows the last four characters of the one in use, never the secret.
 */
export function EngagementSetupTab() {
  const api = useApiClient();
  const setup = useApi<EngagementSetup>((signal) => api.engagementSetup(signal), []);

  return (
    <div className={styles.wrap}>
      <CardHeader
        title="Mailbox apps"
        subtitle="The Google and Microsoft apps SDRs connect Gmail and Outlook through. Enter the ids and secrets here, then paste the addresses below into Google Cloud and Azure."
      />
      <DataState
        state={setup}
        errorTitle="Couldn't load the mailbox app setup"
        skeleton={<Skeleton width="100%" height={320} />}
      >
        {(s) => {
          const google = s.mailbox_apps.find((a) => a.provider === "google");
          const microsoft = s.mailbox_apps.find((a) => a.provider === "microsoft");
          return (
            <>
              <BaseUrlSection setup={s} onSaved={setup.refetch} />
              {google && <GoogleSection app={google} setup={s} onSaved={setup.refetch} />}
              {microsoft && <MicrosoftSection app={microsoft} setup={s} onSaved={setup.refetch} />}

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
          );
        }}
      </DataState>
    </div>
  );
}

/* ---- one save per section ------------------------------------------------------------------ */

/**
 * Local copies of a section's fields, and a save that sends only what changed. A secret is sent
 * only when something was typed into it: the box is always empty, and posting it blank must not
 * read as "remove the secret" (the server keeps a blank secret too, by the same rule).
 */
function useSectionForm<K extends keyof EngagementSetupInput>(
  initial: Record<K, string>, secrets: readonly (keyof EngagementSetupInput)[], onSaved: () => void,
  what: string,
) {
  const api = useApiClient();
  const toast = useToast();
  const [values, setValues] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = JSON.stringify(initial);
  // Re-seed when the server's values change (after a save, or a change on another tab).
  useEffect(() => setValues(initial), [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const changed = (Object.keys(values) as K[]).filter((k) =>
    secrets.includes(k) ? values[k].trim() !== "" : values[k].trim() !== initial[k]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (changed.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const body: EngagementSetupInput = {};
      for (const k of changed) body[k] = values[k].trim();
      await api.saveEngagementSetup(body);
      toast.success(`${what} saved`, "Live within 30 seconds across the API and the worker.");
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  const set = (k: K) => (e: { target: { value: string } }) =>
    setValues((v) => ({ ...v, [k]: e.target.value }));
  return { values, set, save, saving, error, dirty: changed.length > 0, isChanged: (k: K) => changed.includes(k) };
}

function SectionForm({
  id, title, badge, children, onSubmit, saving, dirty, error, saveLabel,
}: {
  id: string; title: string; badge?: ReactNode; children: ReactNode;
  onSubmit: (e: FormEvent) => void; saving: boolean; dirty: boolean; error: string | null;
  saveLabel: string;
}) {
  return (
    <section className={styles.section} aria-labelledby={id}>
      <h3 id={id} className={styles.heading}>{title}{badge}</h3>
      <form className={styles.form} onSubmit={onSubmit} noValidate>
        {children}
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.actions}>
          <Button type="submit" loading={saving} disabled={!dirty}>{saveLabel}</Button>
        </div>
      </form>
    </section>
  );
}

function secretPlaceholder(hint: string | undefined, where: string): string {
  return hint ? `Stored, ends in ${hint}. Paste a new one to replace it.` : `Paste the client secret from ${where}`;
}

function configuredBadge(app: MailboxAppSetup) {
  return (
    <Badge tone={app.configured ? "success" : "warning"}>
      {app.configured ? "Configured" : "Not configured"}
    </Badge>
  );
}

function BaseUrlSection({ setup, onSaved }: { setup: EngagementSetup; onSaved: () => void }) {
  const f = useSectionForm({ public_base_url: setup.public_base_url }, [], onSaved, "Public base URL");
  return (
    <SectionForm id="eng-base" title="Public base URL" onSubmit={f.save} saving={f.saving}
      dirty={f.dirty} error={f.error} saveLabel="Save base URL"
      badge={<Badge tone={setup.public_base_url ? "success" : "warning"}>
        {setup.public_base_url ? "Set" : "Not set"}</Badge>}>
      <Field
        label="Base URL"
        hint={setup.public_base_url && f.isChanged("public_base_url")
          ? "Changing it breaks the redirect URIs and notification URLs already registered in Google Cloud and Azure until you update them there."
          : "This deployment's https address. Every URL below is built from it."}
      >
        <Input type="url" inputMode="url" value={f.values.public_base_url}
          onChange={f.set("public_base_url")} placeholder="https://app.example.com" />
      </Field>
      <p className={styles.note}>
        Campaigns and reply desk:{" "}
        <Badge tone={setup.campaigns_enabled ? "success" : "neutral"}>
          {setup.campaigns_enabled ? "On" : "Off"}
        </Badge>{" "}
        (switched under Configuration)
      </p>
    </SectionForm>
  );
}

function GoogleSection({ app, setup, onSaved }: {
  app: MailboxAppSetup; setup: EngagementSetup; onSaved: () => void;
}) {
  const f = useSectionForm({
    google_client_id: app.client_id,
    google_client_secret: "",
    google_pubsub_topic: setup.gmail_pubsub_topic,
    google_push_service_account: setup.gmail_push_service_account,
  }, ["google_client_secret"], onSaved, "Google settings");
  return (
    <SectionForm id="eng-google" title="Google (Gmail)" badge={configuredBadge(app)}
      onSubmit={f.save} saving={f.saving} dirty={f.dirty} error={f.error}
      saveLabel="Save Google settings">
      <MissingList app={app} />
      <div className={styles.grid}>
        <Field label="OAuth client id"
          hint={app.client_id && f.isChanged("google_client_id")
            ? "A different client signs out every connected Gmail mailbox; each SDR connects again."
            : "Google Cloud → APIs & Services → Credentials."}>
          <Input value={f.values.google_client_id} onChange={f.set("google_client_id")}
            placeholder="123456789012-abc123.apps.googleusercontent.com" autoComplete="off" />
        </Field>
        <Field label="Client secret" hint="Stored sealed. It is never shown again.">
          <Input type="password" value={f.values.google_client_secret}
            onChange={f.set("google_client_secret")} autoComplete="new-password"
            placeholder={secretPlaceholder(app.secret_hint, "Google Cloud")} />
        </Field>
        <Field label="Gmail notification topic"
          hint="Optional. Replies arrive in seconds instead of on the few-minute poll.">
          <Input value={f.values.google_pubsub_topic} onChange={f.set("google_pubsub_topic")}
            placeholder="projects/my-project/topics/gmail-replies" autoComplete="off" />
        </Field>
        <Field label="Push service account"
          hint="The account the Pub/Sub push subscription signs with.">
          <Input value={f.values.google_push_service_account}
            onChange={f.set("google_push_service_account")} autoComplete="off"
            placeholder="gmail-push@my-project.iam.gserviceaccount.com" />
        </Field>
      </div>
      <PasteList title="Paste into Google Cloud" rows={[
        ["Authorised redirect URI", app.redirect_uri],
        ["Scopes", app.scopes.join(" ")],
        ["Push endpoint (also the OIDC audience)", setup.gmail_push_audience],
      ]} />
    </SectionForm>
  );
}

function MicrosoftSection({ app, setup, onSaved }: {
  app: MailboxAppSetup; setup: EngagementSetup; onSaved: () => void;
}) {
  const f = useSectionForm({
    microsoft_client_id: app.client_id,
    microsoft_client_secret: "",
    microsoft_tenant: app.tenant === "common" ? "" : app.tenant,
  }, ["microsoft_client_secret"], onSaved, "Microsoft settings");
  return (
    <SectionForm id="eng-microsoft" title="Microsoft 365 (Outlook)" badge={configuredBadge(app)}
      onSubmit={f.save} saving={f.saving} dirty={f.dirty} error={f.error}
      saveLabel="Save Microsoft settings">
      <MissingList app={app} />
      <div className={styles.grid}>
        <Field label="Application (client) id"
          hint={app.client_id && f.isChanged("microsoft_client_id")
            ? "A different app signs out every connected Outlook mailbox; each SDR connects again."
            : "Azure → App registrations → your app → Overview."}>
          <Input value={f.values.microsoft_client_id} onChange={f.set("microsoft_client_id")}
            placeholder="00000000-0000-0000-0000-000000000000" autoComplete="off" />
        </Field>
        <Field label="Client secret" hint="The secret's VALUE, not its id. Stored sealed.">
          <Input type="password" value={f.values.microsoft_client_secret}
            onChange={f.set("microsoft_client_secret")} autoComplete="new-password"
            placeholder={secretPlaceholder(app.secret_hint, "Azure")} />
        </Field>
        <Field label="Tenant"
          hint="Blank means common: any work, school or personal account. Or organizations, or one tenant id or domain.">
          <Input value={f.values.microsoft_tenant} onChange={f.set("microsoft_tenant")}
            placeholder="common" autoComplete="off" />
        </Field>
      </div>
      <PasteList title="Paste into Azure" rows={[
        ["Redirect URI (Web)", app.redirect_uri],
        ["API permissions", app.scopes.join(" ")],
        ["Notification URL", setup.graph_notification_url],
      ]} />
    </SectionForm>
  );
}

function MissingList({ app }: { app: MailboxAppSetup }) {
  if (app.missing.length === 0) return null;
  const label = app.provider === "google" ? "Google" : "Microsoft";
  return (
    <ul className={styles.missingList} aria-label={`${label}: still missing`}>
      {app.missing.map((m) => <li key={m}>{m.replace(/ \(Control plane → Mailbox apps\)$/, "")}</li>)}
    </ul>
  );
}

function PasteList({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div className={styles.paste}>
      <h4 className={styles.pasteTitle}>{title}</h4>
      {rows.map(([label, value]) => <CopyRow key={label} label={label} value={value} />)}
      {rows.some(([, v]) => !v) && (
        <p className={styles.note}>Save the public base URL first; these addresses are built from it.</p>
      )}
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
