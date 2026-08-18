import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Field, Modal } from "@/components/ui";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { usePlatformCan } from "@/app/RequirePlatformAdmin";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import type { UserActivity } from "@/lib/types";
import { USERS_IMPERSONATE, USERS_MANAGE } from "@/lib/permissions";
import styles from "./UserActionsDialog.module.css";

/**
 * Platform-admin actions against one user account.
 *
 * The whole `admin_users` router shipped working, audited, permission-gated — and with **zero
 * callers**. Suspend, reactivate, MFA reset and impersonation were reachable only by someone who
 * knew the URL, which is the same way the billing control plane was stranded before it got a nav
 * entry.
 *
 * Every action here is destructive-adjacent and takes a **reason**, because the server records one
 * in `billing_audit_log` and a suspension nobody can explain three months later is the one that
 * gets reversed by mistake. Impersonation's reason is mandatory server-side (min 8 chars); the
 * others are optional but still asked for, because the moment to capture intent is now.
 *
 * Actions are grouped by consequence, not by endpoint: recovery (MFA) is routine support work,
 * access (suspend) changes what someone can do, and impersonation reads their data. They should
 * not look equally casual.
 */
interface Props {
  open: boolean;
  onClose: () => void;
  /** Empty opens the dialog with a blank email field, so it works without a user list. */
  initialEmail?: string;
}

export function UserActionsDialog({ open, onClose, initialEmail = "" }: Props) {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const can = usePlatformCan();
  const { beginImpersonation } = useAuth();

  const [email, setEmail] = useState(initialEmail);
  const [reason, setReason] = useState("");
  const [ttl, setTtl] = useState(30);
  const [busy, setBusy] = useState<string | null>(null);
  const [activity, setActivity] = useState<UserActivity | null>(null);
  const [loadingActivity, setLoadingActivity] = useState(false);

  const target = email.trim().toLowerCase();
  const canManage = can(USERS_MANAGE);
  const canImpersonate = can(USERS_IMPERSONATE);
  // The server enforces this; mirroring it here turns a 422 into a disabled button.
  const reasonTooShort = reason.trim().length < 8;

  async function loadActivity() {
    if (!target) return;
    setLoadingActivity(true);
    setActivity(null);
    try {
      setActivity(await api.userActivity(target));
    } catch (err) {
      fail(`Couldn't load activity for ${target}`, err);
    } finally {
      setLoadingActivity(false);
    }
  }

  function fail(what: string, err: unknown) {
    toast.error(what, err instanceof ApiError ? err.detail : "Please try again.");
  }

  async function run(kind: string, fn: () => Promise<string>) {
    if (!target) return;
    setBusy(kind);
    try {
      toast.success(await fn());
    } catch (err) {
      fail(`Couldn't ${kind} ${target}`, err);
    } finally {
      setBusy(null);
    }
  }

  async function impersonate() {
    if (!target || reasonTooShort) return;
    setBusy("impersonate");
    try {
      const session = await api.impersonateUser(target, reason.trim(), ttl);
      beginImpersonation(session);
      onClose();
      // Land on the dashboard: the admin wants the customer's view, and staying on the staff
      // console under a read-only tenant token shows nothing but 403s.
      navigate("/dashboard");
      toast.success(`Viewing as ${target}`, `Read-only, ${session.expires_in_min} minutes.`);
    } catch (err) {
      fail(`Couldn't impersonate ${target}`, err);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="User administration">
      <div className={styles.body}>
        <Field
          label="User email"
          hint="The account these actions apply to."
        >
          <input
            type="email"
            value={email}
            autoComplete="off"
            placeholder="person@customer.com"
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>

        <Field
          label="Reason"
          hint="Recorded in the audit log. Required to impersonate (8 characters or more)."
        >
          <input
            type="text"
            value={reason}
            placeholder="Ticket #1234 — customer cannot log in"
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>

        {canManage && (
          <section className={styles.group}>
            <h3 className={styles.groupTitle}>Account recovery</h3>
            <p className={styles.groupNote}>
              Clears every MFA factor and recovery code. The user sets up MFA again at next login.
              Use when someone has lost their authenticator and their codes.
            </p>
            <Button
              variant="secondary"
              disabled={!target}
              loading={busy === "reset MFA for"}
              onClick={() =>
                run("reset MFA for", async () => {
                  await api.resetUserMfa(target);
                  return `MFA cleared for ${target}`;
                })
              }
            >
              Clear MFA factors
            </Button>
          </section>
        )}

        {canManage && (
          <section className={styles.group}>
            <h3 className={styles.groupTitle}>Access</h3>
            <p className={styles.groupNote}>
              Suspending stops this person logging in anywhere, in every workspace they belong to.
              Their data is untouched and reactivating restores access.
            </p>
            <div className={styles.row}>
              <Button
                variant="danger"
                disabled={!target}
                loading={busy === "suspend"}
                onClick={() =>
                  run("suspend", async () => {
                    await api.suspendUser(target, reason.trim());
                    return `${target} suspended`;
                  })
                }
              >
                Suspend user
              </Button>
              <Button
                variant="secondary"
                disabled={!target}
                loading={busy === "reactivate"}
                onClick={() =>
                  run("reactivate", async () => {
                    await api.reactivateUser(target);
                    return `${target} reactivated`;
                  })
                }
              >
                Reactivate
              </Button>
            </div>
          </section>
        )}

        {canImpersonate && (
          <section className={styles.group}>
            <h3 className={styles.groupTitle}>Impersonate</h3>
            <p className={styles.groupNote}>
              Opens a time-boxed, <strong>read-only</strong> session as this user. Every change is
              refused by the server, and the whole session is attributed to you in the audit log.
            </p>
            <div className={styles.row}>
              <Field label="Minutes" className={styles.ttl}>
                <input
                  type="number"
                  min={1}
                  max={120}
                  value={ttl}
                  onChange={(e) => setTtl(Number(e.target.value))}
                />
              </Field>
              <Button
                disabled={!target || reasonTooShort}
                loading={busy === "impersonate"}
                onClick={impersonate}
              >
                View as this user
              </Button>
            </div>
            {target && reasonTooShort && (
              <p className={styles.blocked}>
                A reason of at least 8 characters is required before impersonating.
              </p>
            )}
          </section>
        )}

        {canManage && (
          <section className={styles.group}>
            <h3 className={styles.groupTitle}>Activity</h3>
            <p className={styles.groupNote}>
              What this person has done, plus anything staff have done to their account.
            </p>
            <div>
              <Button
                variant="secondary"
                disabled={!target}
                loading={loadingActivity}
                onClick={loadActivity}
              >
                Load activity
              </Button>
            </div>

            {activity && (
              <div className={styles.activity}>
                <p className={styles.meta}>
                  {activity.suspended ? (
                    <>
                      <strong>Suspended</strong>
                      {activity.suspended_reason ? ` — ${activity.suspended_reason}` : ""}
                    </>
                  ) : (
                    "Active"
                  )}
                  {activity.memberships.length > 0 && (
                    <>
                      {" · "}
                      {activity.memberships
                        .map((m) => `${m.tenant_name} (${m.role})`)
                        .join(", ")}
                    </>
                  )}
                </p>

                {/* The attribution caveat sits ABOVE the lists, not in a footnote: an operator who
                    reads "no activity" and stops has been misled, because most product actions
                    carry no user id at all. */}
                <p className={styles.caveat}>{activity.attribution_note}</p>

                <h4 className={styles.subTitle}>
                  Their actions ({activity.metered_actions.length})
                </h4>
                {activity.metered_actions.length === 0 ? (
                  <p className={styles.groupNote}>
                    Nothing recorded against this user. That is not the same as having done
                    nothing.
                  </p>
                ) : (
                  <ul className={styles.list}>
                    {activity.metered_actions.slice(0, 12).map((a, i) => (
                      <li key={`${a.capability_id}-${i}`}>
                        <code>{a.capability_id}</code>
                        <span className={styles.when}>
                          {a.occurred_at ? new Date(a.occurred_at).toLocaleString() : "—"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                <h4 className={styles.subTitle}>
                  Staff actions on this account ({activity.admin_actions.length})
                </h4>
                {activity.admin_actions.length === 0 ? (
                  <p className={styles.groupNote}>None.</p>
                ) : (
                  <ul className={styles.list}>
                    {activity.admin_actions.slice(0, 12).map((a, i) => (
                      <li key={`${a.action}-${i}`}>
                        <code>{a.action}</code>
                        {a.note && <span className={styles.note}>{a.note}</span>}
                        <span className={styles.when}>
                          {a.at ? new Date(a.at).toLocaleString() : "—"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </section>
        )}

        {!canManage && !canImpersonate && (
          <p className={styles.groupNote}>
            Your platform role does not include user administration.
          </p>
        )}
      </div>
    </Modal>
  );
}
