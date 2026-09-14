import { useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Badge, Button, Card, CardHeader, Icons, Skeleton, useToast } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import { easeOut } from "@/lib/motion";
import type { BillingUsage, SellablePlan } from "@/lib/types";
import {
  annualBenefits,
  currentPlan,
  defaultInterval,
  formatPrice,
  groupFamilies,
  perMonthCents,
  shownPlan,
} from "./planFamilies";
import type { AnnualBenefits, BillingInterval, PlanFamily } from "./planFamilies";
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
 *
 * ONE CARD PER TIER. An annual plan is its own row, and rendering a card per row put "Launch" and
 * "Launch (annual)" side by side as if they were different products. Cards are grouped on the
 * server's `family`, and one Annual | Monthly switch decides which row every card shows; checkout
 * buys exactly the row on screen.
 */
export function PlanPicker({ usage }: { usage: BillingUsage | null }) {
  const api = useApiClient();
  const toast = useToast();
  const plans = useApi<SellablePlan[]>((signal) => api.billingPlans(signal), []);
  const [busy, setBusy] = useState<string | null>(null);
  // Null until someone uses the switch. Until then the view follows `defaultInterval`, which needs
  // the price list, so it is derived on every render rather than copied into state by an effect.
  const [chosen, setChosen] = useState<BillingInterval | null>(null);
  const [announcement, setAnnouncement] = useState("");

  const families = useMemo(() => groupFamilies(plans.data ?? []), [plans.data]);
  const interval = chosen ?? defaultInterval(families);
  // A switch that changes no card is noise, so it only appears when some tier is sold both ways.
  const switchable = families.some((f) => f.monthly !== null && f.annual !== null);
  const savings = families
    .map((f) => (f.monthly && f.annual ? annualBenefits(f.monthly, f.annual).savedPercent : 0))
    .filter((pct) => pct > 0);
  const bestSaving = savings.length > 0 ? Math.max(...savings) : 0;
  const savingHint =
    bestSaving === 0
      ? null
      : savings.every((pct) => pct === bestSaving)
        ? `Save ${bestSaving}% with annual billing`
        : `Save up to ${bestSaving}% with annual billing`;

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

  function choose(next: BillingInterval) {
    setChosen(next);
    // The switch announces its own state; this says what changed on the rest of the screen.
    setAnnouncement(next === "year" ? "Showing annual prices." : "Showing monthly prices.");
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
      {plans.data && families.length === 0 && (
        <p className={styles.contract}>
          No plans are on sale right now. Your current plan and usage above are unaffected.
        </p>
      )}

      {families.length > 0 && (
        <>
          {switchable && (
            <div className={styles.toolbar}>
              <IntervalSwitch value={interval} onChange={choose} />
              {savingHint && <p className={styles.saveHint}>{savingHint}</p>}
            </div>
          )}
          <p className="sr-only" role="status">
            {announcement}
          </p>
          <ul className={styles.grid}>
            {families.map((family) => (
              <PlanCard
                key={family.key}
                family={family}
                interval={interval}
                animate={chosen !== null}
                busy={busy}
                onCheckout={(planId) => go("checkout", planId)}
              />
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

/** The switch's two positions, annual first because it is the default view. */
const INTERVALS: { value: BillingInterval; label: string }[] = [
  { value: "year", label: "Annual" },
  { value: "month", label: "Monthly" },
];

/**
 * Annual | Monthly, as a radio group: exactly one position is always chosen, Tab lands on the
 * chosen one, and the arrow keys move and select together (the WAI-ARIA radio pattern). Buttons
 * rather than native inputs so the whole 44px segment is the target.
 */
function IntervalSwitch({
  value,
  onChange,
}: {
  value: BillingInterval;
  onChange: (next: BillingInterval) => void;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = index;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (index + 1) % INTERVALS.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp")
      next = (index - 1 + INTERVALS.length) % INTERVALS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = INTERVALS.length - 1;
    else return;
    e.preventDefault();
    onChange(INTERVALS[next].value);
    refs.current[next]?.focus();
  }

  return (
    <div role="radiogroup" aria-label="Billing interval" className={styles.switch}>
      {INTERVALS.map((item, i) => {
        const checked = item.value === value;
        return (
          <button
            key={item.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            className={cn(styles.segment, checked && styles.segmentOn)}
            onClick={() => onChange(item.value)}
            onKeyDown={(e) => onKeyDown(e, i)}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}

function PlanCard({
  family,
  interval,
  animate,
  busy,
  onCheckout,
}: {
  family: PlanFamily;
  interval: BillingInterval;
  /** True once the switch has been used: the price only moves when someone moved it. */
  animate: boolean;
  busy: string | null;
  onCheckout: (planId: string) => void;
}) {
  const reduceMotion = useReducedMotion();
  const noteId = useId();
  const plan = shownPlan(family, interval);
  const current = currentPlan(family);
  const onShownPlan = current !== null && current.id === plan.id;
  const benefits =
    family.monthly && family.annual ? annualBenefits(family.monthly, family.annual) : null;
  // The tier's own name and words. An annual row's name and description are written about billing,
  // and the seeded ones understate the saving that the benefits below compute from the prices.
  const tier = family.monthly ?? plan;
  // Set when this tier has no row for the chosen interval, so the card is showing the other one.
  const soleInterval = plan.base_price_cents > 0 && plan.interval !== interval ? plan.interval : null;

  let note: string | null = null;
  if (current !== null && !onShownPlan) {
    const billed = current.interval === "year" ? "annually" : "monthly";
    const change =
      plan.interval !== "year"
        ? "You can switch to monthly billing."
        : benefits && benefits.savedCents > 0
          ? `Switch to annual billing to save ${formatPrice(benefits.savedCents, plan.currency)} a year.`
          : "You can switch to annual billing.";
    note = `You're on ${tier.name}, billed ${billed}. ${change}`;
  } else if (onShownPlan && plan.interval !== "year" && benefits && benefits.savedCents > 0) {
    note = `Annual billing would save you ${formatPrice(benefits.savedCents, plan.currency)} a year on this plan.`;
  }

  return (
    <li className={cn(styles.plan, current !== null && styles.planCurrent)}>
      <div className={styles.planHead}>
        <h3 className={styles.planName}>{tier.name}</h3>
        {current !== null && <Badge tone="success">Current</Badge>}
      </div>

      {/* Keyed by plan, so the block re-enters only when the switch changed what this card shows:
          a quick rise that says "these numbers are new", skipped entirely under reduced motion. */}
      <motion.div
        key={plan.id}
        className={styles.priceBlock}
        initial={animate && !reduceMotion ? { opacity: 0, y: 4 } : false}
        animate={{ opacity: 1, y: 0 }}
        transition={easeOut}
      >
        <Price plan={plan} />
        {soleInterval && (
          <p className={styles.billed}>
            {soleInterval === "year" ? "Only sold with annual billing" : "Only sold with monthly billing"}
          </p>
        )}
      </motion.div>

      <p className={styles.desc}>{tier.description}</p>

      {plan.interval === "year" && family.monthly && family.annual && benefits && (
        <AnnualBenefitList benefits={benefits} monthly={family.monthly} annual={family.annual} />
      )}

      <dl className={styles.facts}>
        <div>
          <dt>Seats</dt>
          <dd>{plan.max_seats ?? "Unlimited"}</dd>
        </div>
        <div>
          <dt>Included usage</dt>
          <dd>{creditsLabel(plan)}</dd>
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
        {note && (
          <p id={noteId} className={styles.note}>
            {note}
          </p>
        )}
        {onShownPlan ? (
          <Button
            variant="secondary"
            size="lg"
            disabled
            aria-describedby={note ? noteId : undefined}
          >
            Your plan
          </Button>
        ) : (
          <Button
            size="lg"
            onClick={() => onCheckout(plan.id)}
            loading={busy === plan.id}
            disabled={busy !== null}
            aria-describedby={note ? noteId : undefined}
          >
            Continue to checkout
          </Button>
        )}
      </div>
    </li>
  );
}

/**
 * The price as a customer compares it. An annual plan leads with its monthly equivalent, because
 * that is the number that sits beside a monthly price, and states what is actually charged directly
 * under it rather than in small print. Screen readers get both as one sentence.
 */
function Price({ plan }: { plan: SellablePlan }) {
  if (plan.base_price_cents === 0) {
    return (
      <p className={styles.price}>
        <span className={styles.amount}>Free</span>
      </p>
    );
  }
  const charged = formatPrice(plan.base_price_cents, plan.currency);
  if (plan.interval !== "year") {
    return (
      <p className={styles.price}>
        <span className="sr-only">{`${charged} a month`}</span>
        <span className={styles.amount} aria-hidden="true">
          {charged}
        </span>
        <span className={styles.per} aria-hidden="true">
          /month
        </span>
      </p>
    );
  }
  const monthly = formatPrice(perMonthCents(plan), plan.currency);
  return (
    <>
      <p className={styles.price}>
        <span className="sr-only">{`${monthly} a month, billed yearly at ${charged}`}</span>
        <span className={styles.amount} aria-hidden="true">
          {monthly}
        </span>
        <span className={styles.per} aria-hidden="true">
          /month
        </span>
      </p>
      <p className={styles.billed} aria-hidden="true">
        {charged} billed yearly
      </p>
    </>
  );
}

/** What annual billing adds for this tier, one line per real advantage and nothing otherwise. */
function AnnualBenefitList({
  benefits,
  monthly,
  annual,
}: {
  benefits: AnnualBenefits;
  monthly: SellablePlan;
  annual: SellablePlan;
}) {
  const items: string[] = [];
  if (benefits.savedCents > 0) {
    const amount = formatPrice(benefits.savedCents, annual.currency);
    items.push(
      benefits.savedPercent > 0
        ? `Save ${amount} a year, ${benefits.savedPercent}% less than paying monthly`
        : `Save ${amount} a year compared with paying monthly`,
    );
  }
  if (benefits.unlimitedSeats) {
    items.push(`Unlimited seats instead of ${formatNumber(monthly.max_seats)}`);
  } else if (benefits.extraSeats > 0) {
    items.push(`${formatNumber(annual.max_seats)} seats instead of ${formatNumber(monthly.max_seats)}`);
  }
  if (benefits.extraCredits > 0) {
    items.push(`${formatNumber(benefits.extraCredits)} more credits a year than monthly billing`);
  }
  if (items.length === 0) return null;

  return (
    <ul className={styles.benefits} aria-label="With annual billing">
      {items.map((text) => (
        <li key={text}>
          <span className={styles.benefitIcon} aria-hidden="true">
            <Icons.CheckIcon />
          </span>
          <span>{text}</span>
        </li>
      ))}
    </ul>
  );
}

/** The allowance per billing period. Free keeps its bare count, as it always has. */
function creditsLabel(plan: SellablePlan): string {
  const credits = `${formatNumber(plan.included_credits)} credits`;
  if (plan.base_price_cents === 0) return credits;
  return plan.interval === "year" ? `${credits} a year` : `${credits} a month`;
}
