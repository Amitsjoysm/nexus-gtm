import { useCallback, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  Spinner,
  useToast,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatNumber, humanize } from "@/lib/format";
import type {
  AutomationSettings,
  CRMSyncStatus,
  EmailAccount,
  SignalPreference,
} from "@/lib/types";
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
              <>
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
                <IcpDailyCountControl
                  settings={data}
                  onSaved={(res) => automation.setData(res)}
                />
              </>
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

        <SignalCollectionCard />

        <MailboxesCard />
      </div>
    </div>
  );
}

const EMPTY_MAILBOX = {
  label: "",
  provider: "gmail",
  username: "",
  password: "",
  from_name: "",
  enabled: true,
};

function MailboxesCard() {
  const api = useApiClient();
  const toast = useToast();
  const accounts = useApi<EmailAccount[]>((signal) => api.listEmailAccounts(signal), []);
  // null = closed; "new" = adding; an account = editing it.
  const [editing, setEditing] = useState<EmailAccount | "new" | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function makeDefault(a: EmailAccount) {
    setBusyId(a.id);
    try {
      await api.setDefaultEmailAccount(a.id);
      accounts.refetch();
    } catch (err) {
      toast.error("Couldn't set default", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  async function test(a: EmailAccount) {
    setBusyId(a.id);
    try {
      const res = await api.testEmailAccount(a.id);
      if (res.ok) {
        toast.success("Test email sent", `Check the inbox of ${a.from_email}.`);
        accounts.refetch();
      } else {
        toast.error("Test failed", res.detail);
      }
    } catch (err) {
      toast.error("Test failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(a: EmailAccount) {
    setBusyId(a.id);
    try {
      await api.deleteEmailAccount(a.id);
      toast.success("Mailbox removed", a.label || a.from_email);
      accounts.refetch();
    } catch (err) {
      toast.error("Couldn't remove", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card padding="lg">
      <CardHeader
        title="Sending mailboxes (SMTP)"
        subtitle="Send approved outreach from your own Gmail or Outlook mailboxes. Add more than one to send from different reps. Nothing sends until you approve it."
        actions={
          <Button
            size="sm"
            variant="secondary"
            iconLeft={<Icons.PlusIcon />}
            onClick={() => setEditing("new")}
          >
            Add mailbox
          </Button>
        }
      />
      <DataState
        state={accounts}
        errorTitle="Couldn't load mailboxes"
        skeleton={<Skeleton width="100%" height={120} />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            compact
            icon={<Icons.SendIcon />}
            title="No mailboxes yet"
            description="Add a Gmail or Outlook mailbox to send approved outreach from your own address."
            action={
              <Button iconLeft={<Icons.PlusIcon />} onClick={() => setEditing("new")}>
                Add mailbox
              </Button>
            }
          />
        }
      >
        {(rows) => (
          <ul className={styles.mbList}>
            {rows.map((a) => (
              <li key={a.id} className={styles.mbRow}>
                <div className={styles.mbInfo}>
                  <span className={styles.mbLabel}>{a.label || a.from_email}</span>
                  <span className={styles.mbEmail}>{a.from_email}</span>
                  <div className={styles.mbBadges}>
                    {a.default && <Badge tone="accent" dot>Default</Badge>}
                    {a.verified_at ? (
                      <Badge tone="success" dot>Verified</Badge>
                    ) : (
                      <Badge tone="warning" dot>Unverified</Badge>
                    )}
                    {!a.enabled && <Badge tone="neutral">Off</Badge>}
                  </div>
                </div>
                <div className={styles.mbActions}>
                  {!a.default && (
                    <Button
                      size="sm"
                      variant="ghost"
                      loading={busyId === a.id}
                      onClick={() => makeDefault(a)}
                    >
                      Make default
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    loading={busyId === a.id}
                    onClick={() => test(a)}
                  >
                    Send test
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setEditing(a)}>
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    loading={busyId === a.id}
                    onClick={() => remove(a)}
                    aria-label={`Remove ${a.label || a.from_email}`}
                  >
                    <Icons.TrashIcon />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </DataState>

      {editing !== null && (
        <MailboxModal
          account={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            accounts.refetch();
          }}
        />
      )}
    </Card>
  );
}

function MailboxModal({
  account,
  onClose,
  onSaved,
}: {
  account: EmailAccount | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [form, setForm] = useState(
    account
      ? {
          label: account.label,
          provider: account.provider || "gmail",
          username: account.username,
          password: "",
          from_name: account.from_name,
          enabled: account.enabled,
        }
      : EMPTY_MAILBOX,
  );
  const [busy, setBusy] = useState(false);

  function set<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  const providerHint =
    form.provider === "gmail"
      ? "Use a Google App Password (Account → Security → App passwords), not your login password."
      : form.provider === "outlook" || form.provider === "office365"
        ? "Use an app password if your account has 2FA enabled."
        : "Enter your SMTP username and password.";

  async function save() {
    setBusy(true);
    try {
      const body = {
        label: form.label.trim(),
        provider: form.provider,
        username: form.username.trim(),
        from_email: form.username.trim(),
        from_name: form.from_name.trim(),
        enabled: form.enabled,
        // Password is write-only: send only when the user typed one.
        ...(form.password ? { password: form.password } : {}),
      };
      if (account) await api.updateEmailAccount(account.id, body);
      else await api.addEmailAccount(body);
      toast.success(account ? "Mailbox updated" : "Mailbox added");
      onSaved();
    } catch (err) {
      toast.error("Couldn't save mailbox", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={account ? "Edit mailbox" : "Add mailbox"}
      description="Approved outreach can send from this address."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={form.username.trim() === ""}>
            {account ? "Save mailbox" : "Add mailbox"}
          </Button>
        </>
      }
    >
      <div className={styles.emailForm}>
        <div className={styles.emailRow}>
          <Field label="Provider">
            <Select
              value={form.provider}
              onChange={(e) => set("provider", e.target.value)}
              options={[
                { value: "gmail", label: "Gmail" },
                { value: "outlook", label: "Outlook.com" },
                { value: "office365", label: "Microsoft 365" },
                { value: "smtp", label: "Custom SMTP" },
              ]}
            />
          </Field>
          <Field label="Label" hint="A name you'll recognize at the approval gate.">
            <Input value={form.label} onChange={(e) => set("label", e.target.value)} placeholder="Jane — Sales" />
          </Field>
        </div>
        <Field label="From name">
          <Input value={form.from_name} onChange={(e) => set("from_name", e.target.value)} placeholder="Jane from Acme" />
        </Field>
        <Field label="Email address (username)">
          <Input
            type="email"
            value={form.username}
            onChange={(e) => set("username", e.target.value)}
            placeholder="you@company.com"
            autoComplete="email"
          />
        </Field>
        <Field label="App password" hint={providerHint}>
          <Input
            type="password"
            value={form.password}
            onChange={(e) => set("password", e.target.value)}
            placeholder={account?.has_password ? "•••••••• (leave blank to keep)" : "App password"}
            autoComplete="new-password"
          />
        </Field>
        <div className={styles.control}>
          <div className={styles.controlText}>
            <span className={styles.controlLabel}>Sending is {form.enabled ? "on" : "off"}</span>
            <span className={styles.controlHint}>
              When on, this mailbox can send approved outreach. Send a test first to verify it.
            </span>
          </div>
          <Switch
            checked={form.enabled}
            disabled={busy}
            label="Enable sending"
            onChange={(v) => set("enabled", v)}
          />
        </div>
      </div>
    </Modal>
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

function IcpDailyCountControl({
  settings,
  onSaved,
}: {
  settings: AutomationSettings;
  onSaved: (s: AutomationSettings) => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [value, setValue] = useState(String(settings.icp_daily_count ?? ""));
  const [saving, setSaving] = useState(false);
  const effective = settings.icp_daily_count ?? settings.icp_daily_default;

  async function save() {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 5 || n > 100) {
      toast.error("Pick a value between 5 and 100");
      return;
    }
    setSaving(true);
    try {
      onSaved(await api.setIcpDailyCount(n));
      toast.success("Daily discovery target saved", `Up to ${n} net-new ICP accounts per day.`);
    } catch (err) {
      toast.error("Couldn't save", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={styles.control}>
      <div className={styles.controlText}>
        <span className={styles.controlLabel}>New ICP accounts per day</span>
        <span className={styles.controlHint}>
          How many net-new accounts that strictly match your ICP the daily discovery adds
          (currently {effective}/day). Needs automation on and an ICP defined on the Relevance
          page.
        </span>
      </div>
      <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
        <Input
          type="number"
          min={5}
          max={100}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={String(settings.icp_daily_default)}
          aria-label="New ICP accounts per day"
          style={{ width: 96 }}
        />
        <Button size="sm" variant="secondary" loading={saving} onClick={save}>
          Save
        </Button>
      </div>
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

/**
 * Which signal kinds this workspace collects.
 *
 * A tester asked why signals they had never enabled were appearing, and whether they were being
 * billed for them. Everything is on by default and turning one off is what stops it being collected
 * and stored — so the copy says that plainly rather than implying it only hides them.
 */
function SignalCollectionCard() {
  const api = useApiClient();
  const toast = useToast();
  const prefs = useApi<SignalPreference[]>((signal) => api.listSignalPreferences(signal), []);
  const [busy, setBusy] = useState<string | null>(null);

  async function toggle(kind: string, enabled: boolean) {
    setBusy(kind);
    try {
      await api.setSignalPreference(kind, enabled);
      await prefs.refetch();
    } catch (err) {
      toast.error(
        "Couldn't save that",
        err instanceof ApiError ? err.detail : "Try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card padding="lg">
      <CardHeader
        title="Signal collection"
        subtitle="Which kinds of buying signal this workspace collects. Turning one off stops it being collected and billed, not just hidden."
      />
      <DataState
        state={prefs}
        errorTitle="Couldn't load signal settings"
        skeleton={<Skeleton width="100%" height={140} />}
      >
        {(rows) => (
          <ul className={styles.signalList}>
            {rows.map((row) => (
              <li key={row.kind} className={styles.signalRow}>
                <label className={styles.signalLabel} htmlFor={`sig-${row.kind}`}>
                  <span>{humanize(row.kind)}</span>
                </label>
                <div className={styles.signalControl}>
                  {busy === row.kind && <Spinner size={16} />}
                  <input
                    id={`sig-${row.kind}`}
                    type="checkbox"
                    checked={row.enabled}
                    disabled={busy === row.kind}
                    onChange={(e) => void toggle(row.kind, e.target.checked)}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </DataState>
    </Card>
  );
}
