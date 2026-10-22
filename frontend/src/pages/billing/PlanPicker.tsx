import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Badge, Button, Card, CardHeader, Icons, Modal, Skeleton, useToast } from "@/components/ui";
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
export function PlanPicker({
  usage,
  onChanged,
}: {
  usage: BillingUsage | null;
  /** Called after a plan switch lands, so the rest of the billing page re-reads. */
  onChanged?: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const plans = useApi<SellablePlan[]>((signal) => api.billingPlans(signal), []);
  const { refetch } = plans;
  // Prices, plans and what the list shows change in Admin without a deploy, and this page is often
  // left open. Re-read the list whenever the page comes back into view (at most every 5 seconds),
  // so nobody buys from yesterday's list (reported 2026-10-09: "changes are not reflecting").
  const lastRead = useRef(Date.now());
  useEffect(() => {
    function refresh() {
      if (document.visibilityState !== "visible" || Date.now() - lastRead.current < 5000) return;
      lastRead.current = Date.now();
      refetch();
    }
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [refetch]);
  const [busy, setBusy] = useState<string | null>(null);
  // Null until someone uses the switch. Until then the view follows `defaultInterval`, which needs
  // the price list, so it is derived on every render rather than copied into state by an effect.
  const [chosen, setChosen] = useState<BillingInterval | null>(null);
  const [announcement, setAnnouncement] = useState("");
  // A workspace already paying through Stripe changes plan on that subscription, after reading
  // the terms here. Checkout would open a second subscription billed beside the first.
  const switchInPlace = usage?.switch_in_place === true;
  // A downgrade Stripe holds until the period ends, or a paid plan set to end then. Either way a
  // switch would be refused until it is cancelled, so the buttons say so instead of failing.
  const scheduled = switchInPlace ? (usage?.scheduled_change ?? null) : null;
  const endsWithPeriod = switchInPlace && usage?.cancel_at_period_end === true;
  const held = scheduled !== null || endsWithPeriod;
  const periodEnd = usage?.period_end ?? null;
  const [confirming, setConfirming] = useState<SellablePlan | null>(null);
  const [switching, setSwitching] = useState(false);
  const currentRow = (plans.data ?? []).find((p) => p.current) ?? null;

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

  async function switchTo(plan: SellablePlan) {
    setSwitching(true);
    try {
      const result = await api.billingSwitchPlan(plan.id);
      if (result.status === "confirm_payment") {
        toast.success("Your bank needs you to confirm the payment", "Opening the invoice to confirm it.");
        // A provider page, not a route: the plan changes once the payment is confirmed there.
        window.location.assign(result.hosted_invoice_url);
        return;
      }
      setConfirming(null);
      toast.success(
        `You're on ${plan.name}`,
        `${formatPrice(result.charged_cents, result.currency)} was charged to the card on file.`,
      );
      refetch();
      onChanged?.();
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Please try again.";
      toast.error("Couldn't switch plan", detail);
    } finally {
      setSwitching(false);
    }
  }

  const [keeping, setKeeping] = useState(false);
  async function keepPlan() {
    setKeeping(true);
    try {
      await api.billingKeepPlan();
      toast.success(
        `You're staying on ${usage?.plan_name ?? "your plan"}`,
        "The scheduled change was cancelled.",
      );
      refetch();
      onChanged?.();
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Please try again.";
      toast.error("Couldn't cancel the scheduled change", detail);
    } finally {
      setKeeping(false);
    }
  }

  // A downgrade takes effect when the period ends. Within one billing interval it is confirmed on
  // Stripe's own page, so the click only opens that page. Yearly to monthly is held by the server
  // itself (Stripe's page would apply it at once), so it is confirmed here first and the page
  // re-reads instead of leaving.
  const [confirmingDowngrade, setConfirmingDowngrade] = useState<SellablePlan | null>(null);
  const heldInApp = (plan: SellablePlan) =>
    currentRow !== null && currentRow.interval === "year" && plan.interval === "month" &&
    plan.base_price_cents > 0;

  async function downgrade(plan: SellablePlan) {
    setBusy(plan.id);
    try {
      const session = await api.billingDowngrade(plan.id);
      if (session.flow === "scheduled") {
        setConfirmingDowngrade(null);
        setBusy(null);
        toast.success(
          `Moving to ${plan.name} ${session.period_end ? `on ${longDate(session.period_end)}` : "when this period ends"}`,
          "Nothing was charged today.",
        );
        refetch();
        onChanged?.();
        return;
      }
      window.location.assign(session.url);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Please try again.";
      toast.error("Couldn't schedule the downgrade", detail);
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
        subtitle={
          switchInPlace
            ? "Upgrades start today. Downgrades start when your current period ends."
            : "Change plan at any time. You are charged for the days you use."
        }
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

      {/* Only before the first answer: a refresh keeps the cards on screen while it reads. */}
      {plans.loading && !plans.data && <Skeleton width="100%" height={200} />}
      {plans.error && !plans.data && (
        <p className={styles.contract}>
          Couldn't load the price list. Your current plan and usage above are unaffected.
        </p>
      )}
      {plans.data && families.length === 0 && (
        <p className={styles.contract}>
          No plans are on sale right now. Your current plan and usage above are unaffected.
        </p>
      )}

      {held && (
        <div className={styles.scheduled} role="status">
          <p className={styles.scheduledText}>
            {scheduled
              ? `Moves to ${scheduled.plan_name} ${scheduled.at ? `on ${longDate(scheduled.at)}` : "when this period ends"}. Until then you keep ${usage?.plan_name ?? "your plan"} and its credits.`
              : `Your paid plan ends ${periodEnd ? `on ${longDate(periodEnd)}` : "when this period ends"}. To change plan, renew it with Manage payment method first.`}
          </p>
          {/* Stripe's Manage billing page shows a scheduled change but cannot cancel it, so the
              way back is here. */}
          {scheduled && (
            <Button
              variant="secondary"
              size="lg"
              onClick={() => void keepPlan()}
              loading={keeping}
              disabled={busy !== null}
            >
              Keep {usage?.plan_name ?? "my plan"}
            </Button>
          )}
        </div>
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
          {/* Columns follow the number of cards, at most four: 4 sit 4-up or 2 by 2, never 3 + 1. */}
          <ul className={styles.grid} data-count={Math.min(families.length, 5)}>
            {families.map((family) => (
              <PlanCard
                key={family.key}
                family={family}
                interval={interval}
                animate={chosen !== null}
                busy={busy}
                switchInPlace={switchInPlace}
                held={held}
                scheduledPlanId={scheduled?.plan_id ?? null}
                periodEnd={periodEnd}
                onCheckout={(planId) => go("checkout", planId)}
                onSwitch={setConfirming}
                onDowngrade={(plan) =>
                  heldInApp(plan) ? setConfirmingDowngrade(plan) : void downgrade(plan)
                }
                confirmsHere={heldInApp}
              />
            ))}
          </ul>
        </>
      )}

      {confirming && (
        <SwitchDialog
          plan={confirming}
          current={currentRow}
          switching={switching}
          onCancel={() => setConfirming(null)}
          onConfirm={() => void switchTo(confirming)}
        />
      )}

      {confirmingDowngrade && (
        <DowngradeDialog
          plan={confirmingDowngrade}
          current={currentRow}
          periodEnd={periodEnd}
          working={busy === confirmingDowngrade.id}
          onCancel={() => setConfirmingDowngrade(null)}
          onConfirm={() => void downgrade(confirmingDowngrade)}
        />
      )}
    </Card>
  );
}

/** A date as the customer reads it, e.g. "Nov 22, 2026". */
function longDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "the end of this period"
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/**
 * The terms of a switch, before any money moves. The new plan starts today and is charged in full
 * now; nothing is refunded and the credits already held stay. Said plainly here because it is the
 * opposite of the proration a customer might expect, and the button names the amount. Only
 * upgrades get here: a downgrade waits for the period to end and is confirmed on Stripe's page.
 */
function SwitchDialog({
  plan,
  current,
  switching,
  onCancel,
  onConfirm,
}: {
  plan: SellablePlan;
  current: SellablePlan | null;
  switching: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const price = formatPrice(plan.base_price_cents, plan.currency);
  return (
    <Modal
      open
      onClose={switching ? () => undefined : onCancel}
      title={`Switch to ${plan.name}?`}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onCancel} disabled={switching}>
            Keep {current?.name ?? "my plan"}
          </Button>
          <Button onClick={onConfirm} loading={switching}>
            Switch and pay {price}
          </Button>
        </>
      }
    >
      <p className={styles.note}>
        {plan.name} starts today and {price} is charged now to the card on file. It then renews
        {plan.interval === "year" ? " every year" : " every month"} from today.
      </p>
      <p className={styles.contract}>
        The rest of your current period is not refunded, and the credits you already have stay in
        your balance. If the payment does not go through, nothing changes.
      </p>
    </Modal>
  );
}

/**
 * Yearly to monthly, confirmed here because no Stripe page confirms it: the server holds it until
 * the paid year ends. Says when it starts, what it costs from then, and that nothing is charged now.
 */
function DowngradeDialog({
  plan,
  current,
  periodEnd,
  working,
  onCancel,
  onConfirm,
}: {
  plan: SellablePlan;
  current: SellablePlan | null;
  periodEnd: string | null;
  working: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const price = formatPrice(plan.base_price_cents, plan.currency);
  const when = periodEnd ? `on ${longDate(periodEnd)}` : "when your current period ends";
  return (
    <Modal
      open
      onClose={working ? () => undefined : onCancel}
      title={`Downgrade to ${plan.name}?`}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onCancel} disabled={working}>
            Keep {current?.name ?? "my plan"}
          </Button>
          <Button onClick={onConfirm} loading={working}>
            Schedule the downgrade
          </Button>
        </>
      }
    >
      <p className={styles.note}>
        {plan.name} starts {when}, at {price} a month from then. Nothing is charged today.
      </p>
      <p className={styles.contract}>
        Until then you keep {current?.name ?? "your plan"} and the credits it came with. You can
        cancel the scheduled change on this page before it starts.
      </p>
    </Modal>
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
  switchInPlace,
  held,
  scheduledPlanId,
  periodEnd,
  onCheckout,
  onSwitch,
  onDowngrade,
  confirmsHere,
}: {
  family: PlanFamily;
  interval: BillingInterval;
  /** True once the switch has been used: the price only moves when someone moved it. */
  animate: boolean;
  busy: string | null;
  /** The workspace pays through Stripe: dearer plans switch in place, cheaper ones are scheduled. */
  switchInPlace: boolean;
  /** A change is already scheduled, or the paid plan is set to end: nothing else can be chosen. */
  held: boolean;
  /** The plan Stripe moves this workspace to when the period ends, if any. */
  scheduledPlanId: string | null;
  periodEnd: string | null;
  onCheckout: (planId: string) => void;
  onSwitch: (plan: SellablePlan) => void;
  onDowngrade: (plan: SellablePlan) => void;
  /** This downgrade is confirmed on the price list rather than on Stripe's page. */
  confirmsHere: (plan: SellablePlan) => boolean;
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
  // A downgrade waits for the period already paid for, and the customer confirms it on Stripe's
  // page. Said on the card, before the click leaves the app, rather than in a dialog.
  const scheduledHere = scheduledPlanId !== null && scheduledPlanId === plan.id;
  const downgrade = switchInPlace && plan.downgrade && !onShownPlan;
  if (scheduledHere) {
    note = `Moves to ${tier.name} ${periodEnd ? `on ${longDate(periodEnd)}` : "when your current period ends"}.`;
  } else if (downgrade) {
    const when = periodEnd ? ` on ${longDate(periodEnd)}` : "";
    note =
      plan.base_price_cents === 0
        ? `Your paid plan ends when your current period ends${when}. You confirm it on Stripe, and nothing is refunded.`
        : confirmsHere(plan)
          ? `${tier.name} starts when your current period ends${when}. Nothing is charged today.`
          : `${tier.name} starts when your current period ends${when}. You confirm it on Stripe, and nothing is charged today.`;
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
        ) : scheduledHere ? (
          <Button
            variant="secondary"
            size="lg"
            disabled
            aria-describedby={note ? noteId : undefined}
          >
            Scheduled
          </Button>
        ) : downgrade ? (
          // Secondary: the dearer plans keep the primary action on a price list.
          <Button
            variant="secondary"
            size="lg"
            onClick={() => onDowngrade(plan)}
            loading={busy === plan.id}
            disabled={busy !== null || held}
            aria-describedby={note ? noteId : undefined}
          >
            Downgrade to {tier.name}
          </Button>
        ) : switchInPlace && plan.base_price_cents > 0 ? (
          <Button
            size="lg"
            onClick={() => onSwitch(plan)}
            disabled={busy !== null || held}
            aria-describedby={note ? noteId : undefined}
          >
            Switch to {tier.name}
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
