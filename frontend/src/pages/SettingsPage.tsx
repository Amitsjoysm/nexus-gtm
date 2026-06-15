import { useCallback, useEffect, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Field,
  Icons,
  Input,
  Select,
  Skeleton,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatNumber, humanize } from "@/lib/format";
import type { AutomationSettings, CRMSyncStatus, EmailSettings } from "@/lib/types";
import styles from "./SettingsPage.module.css";

export function SettingsPage() {
  const api = useApiClient();
  const toast = useToast();
  const automation = useApi<AutomationSettings>((signal) => api.getAutomation(signal), []);
  const crm = useApi<CRMSyncStatus>((signal) => api.crmSyncStatus(signal), []);
  const [saving, setSaving] = useState(false);

  const toggleAutomation = useCallback(
    async (next: boolean) => {
      setSaving(true);
      try {
        const res = await api.setAutomation(next);
        automation.setData(res);
        crm.refetch(); // CRM "enabled" composes from the workspace automation flag
        toast.success(next ? "Automation turned on" : "Automation turned off");
      } catch (err) {
        toast.error(
          "Couldn't update automation",
          err instanceof ApiError ? err.detail : "Please try again.",
        );
      } finally {
        setSaving(false);
      }
    },
    [api, automation, crm, toast],
  );

  return (
    <div>
      <PageHeader
        title="Settings"
        description="Workspace automation and the health of your connected systems."
      />

      <div className={styles.stack}>
        <Card padding="lg">
          <CardHeader
            title="Continuous automation"
            subtitle="Keep accounts fresh and run recurring drivers on a schedule, without manual triggers."
          />
          <DataState
            state={automation}
            errorTitle="Couldn't load automation settings"
            skeleton={<Skeleton width="100%" height={64} />}
          >
            {(data) => (
              <div className={styles.control}>
                <div className={styles.controlText}>
                  <span className={styles.controlLabel}>
                    Automation is {data.automation_enabled ? "on" : "off"}
                  </span>
                  <span className={styles.controlHint}>
                    When on, the worker re-scores stale accounts and advances cadences each tick.
                    When off, everything waits for a manual run.
                  </span>
                </div>
                <Switch
                  checked={data.automation_enabled}
                  disabled={saving}
                  label="Continuous automation"
                  onChange={toggleAutomation}
                />
              </div>
            )}
          </DataState>
        </Card>

        <Card padding="lg">
          <CardHeader
            title="CRM auto-sync"
            subtitle="Pushes scored accounts, contacts, and new activity to your CRM as they change."
          />
          <DataState
            state={crm}
            errorTitle="Couldn't load sync status"
            skeleton={<Skeleton width="100%" height={96} />}
          >
            {(data) => (
              <div className={styles.crm}>
                <div className={styles.crmHead}>
                  <Badge tone={data.enabled ? "success" : "neutral"} dot>
                    {data.enabled ? "Syncing" : "Paused"}
                  </Badge>
                  <span className={styles.provider}>
                    Provider <strong>{humanize(data.provider)}</strong>
                  </span>
                </div>

                <div className={styles.syncStats}>
                  <Stat label="Up to date" value={data.synced} tone="success" />
                  <Stat label="Pending sync" value={data.pending} />
                </div>

                <p className={styles.note}>
                  {data.enabled
                    ? "Changed accounts sync automatically on the next sweep."
                    : "Auto-sync needs the platform CRM switch on and this workspace's automation enabled."}
                </p>
              </div>
            )}
          </DataState>
        </Card>

        <EmailCard />
      </div>
    </div>
  );
}

function EmailCard() {
  const api = useApiClient();
  const toast = useToast();
  const state = useApi<EmailSettings>((signal) => api.getEmailSettings(signal), []);

  const [provider, setProvider] = useState("gmail");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fromName, setFromName] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [hasPassword, setHasPassword] = useState(false);
  const [verifiedAt, setVerifiedAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);

  // Hydrate the form once the saved settings load.
  const loaded = state.data;
  useEffect(() => {
    if (!loaded) return;
    setProvider(loaded.provider || "gmail");
    setUsername(loaded.username || "");
    setFromName(loaded.from_name || "");
    setEnabled(loaded.enabled);
    setHasPassword(loaded.has_password);
    setVerifiedAt(loaded.verified_at);
  }, [loaded]);

  async function save() {
    setBusy(true);
    try {
      const res = await api.setEmailSettings({
        provider,
        username: username.trim(),
        // Send the password only if the user typed one (write-only on the server).
        ...(password ? { password } : {}),
        from_name: fromName.trim(),
        from_email: username.trim(),
        enabled,
      });
      state.setData(res);
      setPassword("");
      setHasPassword(res.has_password);
      toast.success("Email settings saved");
    } catch (err) {
      toast.error("Couldn't save email settings", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function sendTest() {
    setTesting(true);
    try {
      const res = await api.testEmailSettings();
      if (res.ok) {
        toast.success("Test email sent", "Check the inbox of your account email.");
        setVerifiedAt(new Date().toISOString());
      } else {
        toast.error("Test failed", res.detail);
      }
    } catch (err) {
      toast.error("Test failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setTesting(false);
    }
  }

  const providerHint =
    provider === "gmail"
      ? "Use a Google App Password (Account → Security → App passwords), not your login password."
      : provider === "outlook" || provider === "office365"
        ? "Use an app password if your account has 2FA enabled."
        : "Enter your SMTP username and password.";

  return (
    <Card padding="lg">
      <CardHeader
        title="Outbound email (SMTP)"
        subtitle="Send approved cadence emails from your own Gmail or Outlook mailbox. Nothing sends until you approve a cadence."
      />
      <DataState
        state={state}
        errorTitle="Couldn't load email settings"
        skeleton={<Skeleton width="100%" height={180} />}
      >
        {() => (
          <div className={styles.emailForm}>
            <div className={styles.emailRow}>
              <Field label="Provider">
                <Select
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  options={[
                    { value: "gmail", label: "Gmail" },
                    { value: "outlook", label: "Outlook.com" },
                    { value: "office365", label: "Microsoft 365" },
                    { value: "smtp", label: "Custom SMTP" },
                  ]}
                />
              </Field>
              <Field label="From name">
                <Input value={fromName} onChange={(e) => setFromName(e.target.value)} placeholder="Jane from Acme" />
              </Field>
            </div>
            <Field label="Email address (username)">
              <Input
                type="email"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="you@company.com"
                autoComplete="email"
              />
            </Field>
            <Field label="App password" hint={providerHint}>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={hasPassword ? "•••••••• (leave blank to keep)" : "App password"}
                autoComplete="new-password"
              />
            </Field>

            <div className={styles.control}>
              <div className={styles.controlText}>
                <span className={styles.controlLabel}>Sending is {enabled ? "on" : "off"}</span>
                <span className={styles.controlHint}>
                  {verifiedAt
                    ? "Verified. Approved cadences will send from this mailbox."
                    : "Send a test first to confirm your credentials, then turn sending on."}
                </span>
              </div>
              <Switch
                checked={enabled}
                disabled={busy}
                label="Enable sending"
                onChange={setEnabled}
              />
            </div>

            <div className={styles.emailActions}>
              <Button onClick={save} loading={busy} disabled={username.trim() === ""}>
                Save settings
              </Button>
              <Button
                variant="secondary"
                onClick={sendTest}
                loading={testing}
                disabled={busy || (!hasPassword && password === "")}
              >
                Send test email
              </Button>
              {verifiedAt && (
                <Badge tone="success" dot>
                  Verified
                </Badge>
              )}
            </div>
          </div>
        )}
      </DataState>
    </Card>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "success" }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statLabel}>{label}</span>
      <span className={styles.statValue} data-tone={tone}>
        {formatNumber(value)}
      </span>
    </div>
  );
}

function Switch({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={cn(styles.switch, checked && styles.switchOn)}
      onClick={() => onChange(!checked)}
    >
      <span className={styles.knob} aria-hidden="true">
        {checked && <Icons.CheckIcon />}
      </span>
    </button>
  );
}
