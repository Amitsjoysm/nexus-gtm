import { useState } from "react";
import { Badge, Button, Card, CardHeader, Skeleton, useToast } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { BillingUsage, SellablePlan } from "@/lib/types";
import styles from "./PlanPicker.module.css";

/**
 * The price list, and the button that buys one.
 *
 * `POST /billing/checkout` and `POST /billing/portal` existed server-side and **no screen called
 * either of them**, so a workspace could not change its own plan from inside the product. That gap
 * became load-bearing once locked navigation started routing people here to "view upgrade
 * options" — this page was the promise, and it had no options on it.
 *
 * Nothing here writes a subscription. Checkout returns a provider URL and the new plan arrives
 * later via webhook, which is why the button says "Continue to checkout" rather than "Upgrade":
 * the plan has not changed when the click finishes.
 */
export function PlanPicker({ usage }: { usage: BillingUsage | null }) {
  const api = useApiClient();
  const toast = useToast();
  const plans = useApi<SellablePlan[]>((signal) => api.billingPlans(signal), []);
  const [busy, setBusy] = useState<string | null>(null);

  // An admin-managed deal is not priced on a list, and checkout refuses it with a 409. Saying so
  // is better than showing tiers whose buttons all fail. Read from `plan_class`, which the server
  // decides — matching ADMIN_MANAGED_PLAN_CLASSES rather than guessing from the plan id.
  const adminManaged =
    usage?.plan_class === "custom" || usage?.plan_class === "enterprise";

  async function go(kind: "checkout" | "portal", planId?: string) {
    setBusy(planId ?? "portal");
    try {
      const session =
        kind === "portal" ? await api.billingPortal() : await api.billingCheckout(planId!);
      // A provider redirect, not a route: this leaves the SPA on purpose.
      window.location.assign(session.url);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Please try again.";
      toast.error(
        kind === "portal" ? "Couldn't open the billing portal" : "Couldn't start checkout",
        detail,
      );
      setBusy(null);
    }
  }

  if (adminManaged) {
    return (
      <Card padding="lg">
        <CardHeader
          title="Your plan"
          subtitle="This workspace is on an agreed contract rather than a listed tier."
        />
        <p className={styles.contract}>
          Plan changes, seats and renewal dates are handled by your account team. Self-serve
          checkout is switched off so a portal that knows nothing about your contract cannot
          overwrite it.
        </p>
      </Card>
    );
  }

  return (
    <Card padding="lg">
      <CardHeader
        title="Plans"
        subtitle="Change plan at any time. You are charged for the days you use."
        actions={
          usage?.plan ? (
            <Button
              variant="secondary"
              onClick={() => go("portal")}
              loading={busy === "portal"}
              disabled={busy !== null}
            >
              Manage payment method
            </Button>
          ) : undefined
        }
      />

      {plans.loading && <Skeleton width="100%" height={200} />}
      {plans.error && (
        <p className={styles.contract}>
          Couldn't load the price list. Your current plan and usage above are unaffected.
        </p>
      )}

      {plans.data && (
        <ul className={styles.grid}>
          {plans.data.map((plan) => (
            <li
              key={plan.id}
              className={plan.current ? `${styles.plan} ${styles.planCurrent}` : styles.plan}
            >
              <div className={styles.planHead}>
                <h3 className={styles.planName}>{plan.name}</h3>
                {plan.current && <Badge tone="success">Current</Badge>}
              </div>

              <p className={styles.price}>
                <span className={styles.amount}>
                  {plan.base_price_cents === 0
                    ? "Free"
                    : `$${(plan.base_price_cents / 100).toFixed(0)}`}
                </span>
                {plan.base_price_cents > 0 && (
                  <span className={styles.per}>/{plan.interval}</span>
                )}
              </p>

              <p className={styles.desc}>{plan.description}</p>

              <dl className={styles.facts}>
                <div>
                  <dt>Seats</dt>
                  <dd>{plan.max_seats ?? "Unlimited"}</dd>
                </div>
                <div>
                  <dt>Included usage</dt>
                  <dd>{plan.included_credits.toLocaleString()} credits</dd>
                </div>
              </dl>

              {/* What you get is the decision; what you don't is the one people get wrong after
                  buying. Both are listed, and the excluded list is not hidden behind a toggle. */}
              {plan.includes.length > 0 && (
                <>
                  <p className={styles.listLabel}>Includes</p>
                  <ul className={styles.modules}>
                    {plan.includes.map((m) => (
                      <li key={m}>{m}</li>
                    ))}
                  </ul>
                </>
              )}
              {plan.excludes.length > 0 && (
                <>
                  <p className={styles.listLabel}>Not included</p>
                  <ul className={`${styles.modules} ${styles.modulesOut}`}>
                    {plan.excludes.map((m) => (
                      <li key={m}>{m}</li>
                    ))}
                  </ul>
                </>
              )}

              {/* `lg` (46px), not the default `md` (38px): this is a purchase button, and 38px is
                  under the 44px touch target this design system commits to. */}
              <div className={styles.action}>
                {plan.current ? (
                  <Button variant="secondary" size="lg" disabled>
                    Your plan
                  </Button>
                ) : (
                  <Button
                    size="lg"
                    onClick={() => go("checkout", plan.id)}
                    loading={busy === plan.id}
                    disabled={busy !== null}
                  >
                    Continue to checkout
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
