import type { SellablePlan } from "@/lib/types";

/**
 * One card per tier, from a price list that has one ROW per tier per billing interval.
 *
 * Pairing is the server's decision (`family` / `counterpart_id`, see `interval_pairs` in
 * `nexus/billing/plans.py`). Nothing here reads a plan id or a name to work out which rows belong
 * together: that rule lives where it can be tested against the plans an admin actually authors.
 *
 * Every number a card states about annual billing is computed from plan fields, never written
 * down, so it stays true when a price, a seat cap or an allowance is changed in Admin.
 */

export type BillingInterval = "month" | "year";

export interface PlanFamily {
  /** The server's `family`: the monthly plan's id for a paired tier, else the plan's own id. */
  key: string;
  monthly: SellablePlan | null;
  annual: SellablePlan | null;
}

export function groupFamilies(plans: SellablePlan[]): PlanFamily[] {
  const families = new Map<string, PlanFamily>();
  for (const plan of plans) {
    const family = families.get(plan.family) ?? { key: plan.family, monthly: null, annual: null };
    families.set(plan.family, family);
    // `year` is the only other interval the server accepts; anything else bills monthly, which is
    // also how `next_period_end` reads it.
    if (plan.interval === "year") family.annual = family.annual ?? plan;
    else family.monthly = family.monthly ?? plan;
  }
  // A Map keeps insertion order, so tiers keep the price list's `sort_order`.
  return Array.from(families.values());
}

/** The plan a card shows: the one billed on `interval`, or the only one a single-interval tier has. */
export function shownPlan(family: PlanFamily, interval: BillingInterval): SellablePlan {
  const plan =
    interval === "year" ? (family.annual ?? family.monthly) : (family.monthly ?? family.annual);
  if (plan === null) throw new Error(`plan family "${family.key}" has no plans`);
  return plan;
}

/** The plan in this tier the workspace is on, whichever interval it bills on. */
export function currentPlan(family: PlanFamily): SellablePlan | null {
  if (family.monthly?.current) return family.monthly;
  if (family.annual?.current) return family.annual;
  return null;
}

/**
 * Where the switch starts: ANNUAL, unless the workspace already pays for a tier sold both ways.
 *
 * That exception is about trust, not conversion. A card marked Current showing a price the
 * customer does not pay reads as "did my price change?" on the one screen where money is at stake,
 * and the commonest move from a paid plan (the next tier up) is a like-for-like comparison on the
 * interval they already bill on. The saving is not hidden from them: the switch states it, and so
 * does their own card.
 */
export function defaultInterval(families: PlanFamily[]): BillingInterval {
  for (const family of families) {
    if (family.monthly === null || family.annual === null) continue;
    if (family.monthly.current) return "month";
    if (family.annual.current) return "year";
  }
  return "year";
}

/** A yearly price spread over twelve months, to the nearest cent. */
export function perMonthCents(plan: SellablePlan): number {
  return Math.round(plan.base_price_cents / 12);
}

export interface AnnualBenefits {
  /** Twelve monthly payments minus the annual price. */
  savedCents: number;
  /** `savedCents` as a share of twelve monthly payments, rounded down. */
  savedPercent: number;
  /** Seats the annual plan adds over the monthly one, when both are capped. */
  extraSeats: number;
  /** The annual plan is uncapped where the monthly one is not. */
  unlimitedSeats: boolean;
  /** Credits a year beyond twelve monthly allowances. */
  extraCredits: number;
}

/**
 * What annual billing gets over paying monthly for the same tier.
 *
 * Every field is zero (or false) where annual is not better, so a card can only ever state a real
 * advantage. Credits are compared per YEAR against twelve monthly allowances: on the seeded ladder
 * they are equal, so no "more credits" line appears, and one would only if an admin gave an annual
 * plan a larger allowance.
 *
 * Deliberately silent on WHEN credits arrive. Grants follow the subscription's period, not this
 * row, and a card promising "all up front" would be a claim the price list cannot back.
 */
export function annualBenefits(monthly: SellablePlan, annual: SellablePlan): AnnualBenefits {
  const twelveMonths = monthly.base_price_cents * 12;
  const savedCents = Math.max(0, twelveMonths - annual.base_price_cents);
  return {
    savedCents,
    // Rounded DOWN, so a saving just short of a whole point is never rounded up into a bigger claim.
    savedPercent: twelveMonths > 0 ? Math.floor((savedCents * 100) / twelveMonths) : 0,
    extraSeats:
      monthly.max_seats !== null && annual.max_seats !== null
        ? Math.max(0, annual.max_seats - monthly.max_seats)
        : 0,
    unlimitedSeats: annual.max_seats === null && monthly.max_seats !== null,
    extraCredits: Math.max(0, annual.included_credits - monthly.included_credits * 12),
  };
}

/** Money at the edge. A whole amount drops the cents; anything else keeps both digits. */
export function formatPrice(cents: number, currency: string): string {
  const digits = cents % 100 === 0 ? 0 : 2;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: currency.toUpperCase(),
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(cents / 100);
}
