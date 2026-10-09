import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Field, Input, Skeleton, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { usePlatformCan } from "@/app/RequirePlatformAdmin";
import { ApiError } from "@/lib/api";
import { STRIPE_MANAGE } from "@/lib/permissions";
import type { PaymentCredential, StripeCarryRow } from "@/lib/types";
import styles from "./PaymentsTab.module.css";

/**
 * The Stripe account the platform bills with.
 *
 * Deliberately not part of the generic provider-key pool. A dead search key returns no results and
 * somebody notices within a day; a wrong payment key stops checkout and invoicing, which is
 * indistinguishable from a quiet month. So this screen has a rule that one does not: **a credential
 * cannot go live until a real call against it has succeeded**, and verification reports which
 * ACCOUNT answered, because authenticating against the wrong business looks exactly like success.
 */

const STATUS_TONE: Record<string, "success" | "info" | "danger" | "neutral"> = {
  verified: "success",
  registered: "neutral",
  failed: "danger",
};

const STATUS_LABEL: Record<string, string> = {
  verified: "Verified",
  registered: "Not verified",
  failed: "Failed",
};

/** "3 moved here" plus why any could not be, for the toast after a switch. */
function describeCarry(rows: StripeCarryRow[]): { line: string; problems: string[] } {
  const moved = rows.filter((c) => c.outcome === "carried").length;
  const stuck = rows.filter((c) => c.outcome === "failed");
  const line = rows.length
    ? `Subscriptions moved here: ${moved}${stuck.length ? `, ${stuck.length} not moved` : ""}.`
    : "No subscriptions needed moving.";
  return { line, problems: stuck.map((c) => c.detail ?? "no reason given") };
}

export function PaymentsTab() {
  const api = useApiClient();
  const toast = useToast();
  // Registering the webhook creates an object at Stripe: superadmin only (2026-10-09).
  const canManageStripe = usePlatformCan()(STRIPE_MANAGE);
  const [rows, setRows] = useState<PaymentCredential[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({
    label: "",
    secret_key: "",
    publishable_key: "",
    webhook_secret: "",
  });

  const load = useCallback(async () => {
    setRows(await api.paymentCredentials());
  }, [api]);

  useEffect(() => {
    load().catch(() => setRows([]));
  }, [load]);

  function fail(title: string, err: unknown) {
    toast.error(title, err instanceof ApiError ? err.detail : "Please try again.");
  }

  async function act(id: string, title: string, fn: () => Promise<unknown>) {
    setBusy(id);
    try {
      await fn();
      await load();
    } catch (err) {
      fail(title, err);
    } finally {
      setBusy(null);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setAdding(true);
    try {
      await api.addPaymentCredential({
        label: form.label.trim(),
        secret_key: form.secret_key.trim(),
        publishable_key: form.publishable_key.trim(),
        webhook_secret: form.webhook_secret.trim(),
      });
      setForm({ label: "", secret_key: "", publishable_key: "", webhook_secret: "" });
      await load();
      toast.success("Account added", "Verify it before making it live.");
    } catch (err) {
      fail("Couldn't add the account", err);
    } finally {
      setAdding(false);
    }
  }

  async function verify(rowItem: PaymentCredential) {
    setBusy(rowItem.id);
    try {
      const result = await api.verifyPaymentCredential(rowItem.id);
      await load();
      if (result.ok) {
        toast.success(
          `Connected to ${result.account_name || result.account_id}`,
          result.livemode
            ? "Live mode. Real money will move through this account."
            : "Test mode. No real money moves through this account.",
        );
      } else {
        // Stripe's own words. "Invalid API Key" and "expired" need different fixes, and the status
        // code alone does not tell an operator which one they are looking at.
        toast.error("Stripe refused the key", result.detail);
      }
    } catch (err) {
      fail("Couldn't verify the account", err);
    } finally {
      setBusy(null);
    }
  }

  // Activating a superadmin's key also creates the plans on sale in that Stripe account and moves
  // the previous account's live subscriptions into it; the response says what happened
  // (`stripe_sync`, `stripe_carry`, both absent for anyone without `stripe.manage`).
  async function activate(rowItem: PaymentCredential) {
    setBusy(rowItem.id);
    try {
      const res = await api.activatePaymentCredential(rowItem.id);
      await load();
      const sync = res.stripe_sync ?? null;
      if (!sync) {
        toast.success("Billing account activated");
        return;
      }
      const failed = sync.filter((p) => p.outcome === "failed");
      const created = sync.filter((p) => p.outcome === "created").length;
      const reused = sync.filter((p) => p.outcome === "reused").length;
      const carry = describeCarry(res.stripe_carry ?? []);
      const problems = [
        ...failed.map((p) => `${p.plan_id || "plans"}: ${p.detail ?? "no reason given"}`),
        ...carry.problems,
      ];
      if (problems.length) {
        toast.error("Activated, but not everything moved", problems.join(" · "));
      } else {
        toast.success(
          "Billing account activated",
          `Plans synced to Stripe: ${created} created, ${reused} already there. ${carry.line}`,
        );
      }
    } catch (err) {
      fail("Couldn't activate", err);
    } finally {
      setBusy(null);
    }
  }

  // Creates the endpoint in this credential's Stripe account (pinned API version, the events the
  // app handles) and saves its signing secret here. Replaces the manual step in the dashboard.
  async function registerWebhook(rowItem: PaymentCredential) {
    setBusy(rowItem.id);
    try {
      const res = await api.registerStripeWebhook(rowItem.id);
      await load();
      if (res.others.length) {
        toast.error(
          "Webhook registered, but check Stripe",
          `Another endpoint already points at ${res.url} (${res.others.join(", ")}). Delete it ` +
            "in Stripe, or it will fail every delivery.",
        );
      } else {
        toast.success(
          "Webhook registered in Stripe",
          `${res.events.length} events to ${res.url}. Signing secret saved.` +
            (res.replaced.length ? " The previous endpoint was replaced." : ""),
        );
      }
    } catch (err) {
      fail("Couldn't register the webhook", err);
    } finally {
      setBusy(null);
    }
  }

  // The retry for the move that activation does by itself: a workspace whose old subscription
  // could not be ended then (its old key was missing, or Stripe refused) is tried again.
  async function carryOver(rowItem: PaymentCredential) {
    setBusy(rowItem.id);
    try {
      const res = await api.carryStripeSubscriptions();
      const carry = describeCarry(res.subscriptions);
      if (carry.problems.length) {
        toast.error(carry.line, carry.problems.join(" · "));
      } else {
        toast.success("Subscriptions checked", carry.line);
      }
    } catch (err) {
      fail("Couldn't move the subscriptions", err);
    } finally {
      setBusy(null);
    }
  }

  async function remove(rowItem: PaymentCredential) {
    if (!window.confirm(`Delete the ${rowItem.provider} account ending ${rowItem.key_hint}?`)) {
      return;
    }
    await act(rowItem.id, "Couldn't delete the account", () =>
      api.deletePaymentCredential(rowItem.id));
  }

  const live = (rows ?? []).find((r) => r.active);

  return (
    <div className={styles.stack}>
      <section>
        <h3 className={styles.title}>Billing account</h3>
        <p className={styles.lede}>
          {live ? (
            <>
              Billing through <strong>{live.account_name || live.account_id || live.label}</strong>
              {live.livemode ? " in live mode." : " in test mode."}
            </>
          ) : (
            <>
              No account is active here, so billing uses whatever is in the deployment&rsquo;s
              environment. Adding one takes over.
            </>
          )}
        </p>
      </section>

      <section>
        <h3 className={styles.title}>Add an account</h3>
        <p className={styles.lede}>
          Stored encrypted. A new account starts inactive: verify it first, because a wrong payment
          key does not fail loudly, it stops billing.
        </p>
        <form onSubmit={submit} className={styles.form}>
          <Field label="Label" hint="How you'll recognise it later.">
            <Input
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              placeholder="Marketjoy Ltd — live"
            />
          </Field>
          <Field label="Secret key" hint="Never shown again after saving.">
            <Input
              type="password"
              value={form.secret_key}
              onChange={(e) => setForm({ ...form, secret_key: e.target.value })}
              required
              minLength={8}
              placeholder="sk_live_…"
            />
          </Field>
          <Field
            label="Webhook signing secret"
            hint={canManageStripe
              ? "Leave blank and press Create webhook in Stripe once the key is verified."
              : "From the endpoint in your Stripe dashboard."}
          >
            <Input
              type="password"
              value={form.webhook_secret}
              onChange={(e) => setForm({ ...form, webhook_secret: e.target.value })}
              placeholder="whsec_…"
            />
          </Field>
          <Field label="Publishable key" hint="Public by design; not encrypted.">
            <Input
              value={form.publishable_key}
              onChange={(e) => setForm({ ...form, publishable_key: e.target.value })}
              placeholder="pk_live_…"
            />
          </Field>
          <Button type="submit" loading={adding} disabled={!form.secret_key.trim()}>
            Add account
          </Button>
        </form>
      </section>

      {rows === null && <Skeleton width="100%" height={160} />}

      {rows !== null && rows.length === 0 && (
        <p className={styles.empty}>
          No accounts stored. Billing runs on the environment configuration, exactly as before.
        </p>
      )}

      {rows !== null && rows.length > 0 && (
        <ul className={styles.list}>
          {rows.map((r) => (
            <li key={r.id} className={r.active ? styles.itemLive : styles.item}>
              <div className={styles.head}>
                <span className={styles.label}>{r.label || "(no label)"}</span>
                <code className={styles.hint}>••••{r.key_hint}</code>
                <Badge tone={STATUS_TONE[r.status] ?? "neutral"} dot>
                  {STATUS_LABEL[r.status] ?? r.status}
                </Badge>
                {r.active && <Badge tone="success">Billing with this</Badge>}
                {/* Test and live are the same shape of key and the difference is the whole
                    business. Said in words, not left to a `sk_test_` prefix nobody can see. */}
                {r.status === "verified" && (
                  <Badge tone={r.livemode ? "warning" : "info"}>
                    {r.livemode ? "Live mode" : "Test mode"}
                  </Badge>
                )}
              </div>

              {r.account_name || r.account_id ? (
                <p className={styles.account}>
                  Connected to <strong>{r.account_name || r.account_id}</strong>
                  {r.account_name && r.account_id && (
                    <span className={styles.accountId}> · {r.account_id}</span>
                  )}
                </p>
              ) : (
                <p className={styles.note}>
                  Not verified yet, so we don&rsquo;t know which Stripe account this key belongs to.
                </p>
              )}

              {r.status === "failed" && r.last_error && (
                <p className={styles.error}>{r.last_error}</p>
              )}

              <div className={styles.actions}>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => verify(r)}
                  loading={busy === r.id}
                  disabled={busy !== null}
                >
                  Verify
                </Button>
                {r.active ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      act(r.id, "Couldn't deactivate", () => api.deactivatePaymentCredential(r.id))
                    }
                    disabled={busy !== null}
                  >
                    Stop billing with this
                  </Button>
                ) : (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => void activate(r)}
                    // Unverified is not merely discouraged, it is refused by the server. Disabling
                    // the button says so before the click instead of after it.
                    disabled={busy !== null || r.status !== "verified"}
                    title={
                      r.status === "verified"
                        ? "Bill through this account from now on"
                        : "Verify this account first"
                    }
                  >
                    Make live
                  </Button>
                )}
                {canManageStripe && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => void registerWebhook(r)}
                    disabled={busy !== null || r.status !== "verified"}
                    title={
                      r.status === "verified"
                        ? "Create the webhook in this Stripe account and save its signing secret"
                        : "Verify this account first"
                    }
                  >
                    Create webhook in Stripe
                  </Button>
                )}
                {canManageStripe && r.active && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => void carryOver(r)}
                    disabled={busy !== null}
                    title="Move subscriptions still billed in a previous Stripe account into this one"
                  >
                    Move subscriptions here
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => remove(r)}
                  disabled={busy !== null || r.active}
                  title={r.active ? "Stop billing with it first" : undefined}
                >
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
