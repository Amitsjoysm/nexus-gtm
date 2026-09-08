import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Field, Input } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import type { AlertChannelConnection, AlertChannelSecret } from "@/lib/types";
import { CHANNEL_HELP, CHANNEL_LABEL, fieldMeta, label } from "./vocabulary";
import styles from "./ConnectChannelForm.module.css";

/**
 * Collect one channel's credential.
 *
 * **The fields come from the server**, not from a list here. `CHANNEL_FIELDS` in
 * `nexus/alerts/connections.py` is what both the API and the resolver read, and a form that
 * hard-codes its own copy is how Telegram ends up storing a bot token with no chat id: a row that
 * reads "connected" and delivers nothing.
 *
 * Validation is duplicated from the endpoint on purpose. The server refuses a blank field and a
 * non-https URL either way; doing it here means the user hears about a typo immediately instead of
 * after a round trip.
 */
export function ConnectChannelForm({
  channel,
  canManage,
  onConnected,
  onCancel,
  submitLabel,
  autoFocus,
}: {
  channel: AlertChannelConnection;
  /** Connecting is `manage_workspace`. A rep sees what is needed and who to ask, not a form that
   *  403s on submit. */
  canManage: boolean;
  onConnected: (next: AlertChannelConnection) => void;
  onCancel?: () => void;
  submitLabel?: string;
  autoFocus?: boolean;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [values, setValues] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const name = label(CHANNEL_LABEL, channel.kind);
  const help = CHANNEL_HELP[channel.kind];

  if (!canManage) {
    return (
      <p className={styles.locked} role="status">
        {name} has not been connected yet, and only a workspace owner or admin can connect it. Ask
        one of them to add it under Integrations, then choose it here.
      </p>
    );
  }

  function set(field: string, value: string) {
    setValues((v) => ({ ...v, [field]: value }));
    setErrors((e) => (e[field] ? { ...e, [field]: "" } : e));
  }

  function validate(): boolean {
    const next: Record<string, string> = {};
    for (const field of channel.fields) {
      const value = (values[field] ?? "").trim();
      if (!value) {
        next[field] = `${fieldMeta(field).label} is required.`;
        continue;
      }
      // A webhook URL that is not a URL fails inside a try/except at delivery time, so it would
      // look connected and simply never arrive.
      if (field === "url" && !/^https:\/\//i.test(value)) {
        next[field] = "Must be an https:// address.";
      }
      if (field === "to" && !/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(value)) {
        next[field] = "Enter a valid email address.";
      }
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    setBusy(true);
    try {
      const body: AlertChannelSecret = {};
      for (const field of channel.fields) {
        (body as Record<string, string>)[field] = (values[field] ?? "").trim();
      }
      const next = await api.connectAlertChannel(channel.kind, body);
      setValues({});
      toast.success(`${name} connected`, "Send a test to confirm messages arrive.");
      onConnected(next);
    } catch (err) {
      toast.error(
        `Couldn't connect ${name}`,
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      {help && <p className={styles.help}>{help}</p>}
      {channel.fields.map((field, i) => {
        const meta = fieldMeta(field);
        return (
          <Field key={field} label={meta.label} required error={errors[field] || undefined}>
            <Input
              type={meta.type}
              value={values[field] ?? ""}
              placeholder={meta.placeholder}
              autoComplete="off"
              autoFocus={autoFocus && i === 0}
              onChange={(e) => set(field, e.target.value)}
            />
          </Field>
        );
      })}
      <div className={styles.actions}>
        <Button type="submit" size="sm" loading={busy}>
          {submitLabel ?? `Connect ${name}`}
        </Button>
        {onCancel && (
          <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
      </div>
      <p className={styles.privacy}>
        Stored encrypted. It is never shown again, here or anywhere else in the product.
      </p>
    </form>
  );
}

export default ConnectChannelForm;
