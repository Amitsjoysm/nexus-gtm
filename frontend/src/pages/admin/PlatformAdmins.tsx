import { useState } from "react";
import { Badge, Button, EmptyState, Field, Input, Select, Skeleton, useToast } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { PlatformAdmin } from "@/lib/types";
import styles from "./AdminForms.module.css";

/**
 * Who operates the platform.
 *
 * Separate from tenant membership entirely: granting someone here does not touch any workspace,
 * and being a workspace owner grants nothing here. The email does not need an account yet —
 * pre-authorizing a colleague before they sign up is the normal order.
 */
export function PlatformAdmins() {
  const api = useApiClient();
  const toast = useToast();
  const admins = useApi<PlatformAdmin[]>((signal) => api.listPlatformAdmins(signal), []);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("superadmin");
  const [busy, setBusy] = useState(false);

  async function grant(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const res = await api.grantPlatformAdmin({ email: email.trim(), platform_role: role });
      toast.success(
        res.created ? `${res.email} is now a platform admin` : `${res.email} updated`,
      );
      setEmail("");
      admins.refetch();
    } catch (err) {
      toast.error(
        "Couldn't grant access",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function revoke(target: PlatformAdmin) {
    try {
      await api.revokePlatformAdmin(target.email);
      toast.success(`Revoked ${target.email}`);
      admins.refetch();
    } catch (err) {
      // The server refuses to remove the last active admin; surface that plainly rather than
      // as a generic failure.
      toast.error(
        "Couldn't revoke access",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    }
  }

  return (
    <div className={styles.form}>
      <form onSubmit={grant} className={styles.row}>
        <Field label="Email" hint="They do not need an account yet.">
          <Input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="colleague@company.com"
            required
          />
        </Field>
        <Field label="Role">
          <Select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            options={[
              { value: "superadmin", label: "Superadmin" },
              { value: "finance", label: "Finance" },
              { value: "support", label: "Support" },
            ]}
          />
        </Field>
        <Button type="submit" loading={busy} disabled={!email.trim()}>
          Grant access
        </Button>
      </form>

      <DataState
        state={admins}
        errorTitle="Couldn't load platform admins"
        skeleton={<Skeleton width="100%" height={120} />}
        isEmpty={(d) => d.length === 0}
        empty={
          <EmptyState
            title="No platform admins"
            description="Access is currently granted only through the environment allowlist."
          />
        }
      >
        {(rows) => (
          <div>
            {rows.map((a) => (
              <div key={a.id} className={styles.adminRow}>
                <div className={styles.adminMeta}>
                  <span className={styles.adminEmail}>{a.email}</span>
                  <span className={styles.adminSub}>
                    {a.platform_role}
                    {a.note ? ` · ${a.note}` : ""}
                  </span>
                </div>
                <div className={styles.tenantActions}>
                  <Badge tone={a.active ? "success" : "neutral"}>
                    {a.active ? "active" : "revoked"}
                  </Badge>
                  {a.active && (
                    <Button variant="ghost" onClick={() => revoke(a)}>
                      Revoke
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </DataState>
    </div>
  );
}
