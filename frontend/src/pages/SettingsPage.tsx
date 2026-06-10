import { useCallback, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Card, CardHeader, Icons, Skeleton, useToast } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatNumber, humanize } from "@/lib/format";
import type { AutomationSettings, CRMSyncStatus } from "@/lib/types";
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
      </div>
    </div>
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
