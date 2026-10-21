import { useEffect, useMemo, useState } from "react";
import { Button, Checkbox, Field, Input, Modal, Select, Textarea, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { AdminPlan } from "@/lib/types";
import { formatPrice } from "@/pages/billing/planFamilies";
import styles from "./AdminForms.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
  /** The plan to edit, or null to create a new one. */
  plan: AdminPlan | null;
  /** Every plan, for the "start from" choice when creating. */
  plans: AdminPlan[];
  onDone: () => void;
}

/** Ids the server refuses for a new plan (`nexus/billing/plan_authoring.py`). */
const RESERVED_IDS = ["free", "trial", "enterprise", "internal", "legacy-unlimited"];

/** `Scale Annual` -> `scale-annual`, the same normalisation the server applies. */
function slug(raw: string): string {
  return raw.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
}

function dollars(cents: number | null | undefined): string {
  return cents == null ? "" : String(cents / 100);
}

function cents(text: string): number | null {
  if (text.trim() === "") return null;
  const n = Number(text);
  return Number.isFinite(n) && n >= 0 ? Math.round(n * 100) : null;
}

function whole(text: string): number | null {
  if (text.trim() === "") return null;
  const n = Number(text);
  return Number.isInteger(n) && n >= 0 ? n : null;
}

/** The discount the seeded ladder uses ($99 a month -> $950 a year), and the server's ceiling. */
const DEFAULT_ANNUAL_DISCOUNT = "20";
const MAX_ANNUAL_DISCOUNT = 90;

/**
 * Twelve months less the discount, to the nearest whole unit, halves up. The SAME integer expression
 * as `annual_price_cents` in `nexus/billing/plan_authoring.py`, so the preview quotes exactly the
 * price the server makes the plan at.
 */
function annualCents(monthlyCents: number, pct: number): number {
  return Math.floor((monthlyCents * 12 * (100 - pct) + 5000) / 10000) * 100;
}

/** The whole % off twelve monthly payments an annual price comes to, halves up: the SAME integer
 * expression as `implied_discount_pct` in `plan_authoring.py`, which records it on the plan. */
function impliedPct(monthlyCents: number, annual: number): number {
  const twelve = monthlyCents * 12;
  return twelve <= 0 ? 0 : Math.floor(((twelve - annual) * 200 + twelve) / (2 * twelve));
}

/**
 * Create a plan the price list can sell, or edit a plan's commercial terms.
 *
 * Both server halves shipped on 2026-08-25 (`POST /admin/billing/plans`, `PATCH /plans/{id}`) and no
 * screen ever called them: the Plans tab offered "What's included" and nothing else, so creating a
 * tier or changing a price still meant curl (reported 2026-10-01 as "I can view plans but cannot edit
 * or add one"). The rules stay the server's:
 *
 * - A new plan is a DRAFT, invisible to the price list until it is published from the Plans list.
 * - Its entitlements are cloned from the plan it starts from, never empty: a blank plan would fall
 *   back to permissive catalog defaults and grant nearly everything.
 * - Status is not edited here. Publish and hold go through `PUT /plans/{id}/status`, which refuses to
 *   publish anything but a standard plan; the PATCH body would accept a status and skip that rule.
 */
export function PlanDialog({ open, onClose, plan, plans, onDone }: Props) {
  const api = useApiClient();
  const toast = useToast();
  const creating = plan === null;
  // The same tier at the other interval, which customers may be looking at instead.
  const twin = plan?.counterpart_id
    ? plans.find((p) => p.id === plan.counterpart_id) ?? null
    : null;

  const [name, setName] = useState("");
  const [planId, setPlanId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [basePlan, setBasePlan] = useState("");
  const [billing, setBilling] = useState("month");
  const [price, setPrice] = useState("");
  const [seatPrice, setSeatPrice] = useState("");
  const [credits, setCredits] = useState("");
  const [maxSeats, setMaxSeats] = useState("");
  const [trialDays, setTrialDays] = useState("0");
  const [description, setDescription] = useState("");
  // A monthly tier is sold yearly too, as its own `-annual` plan the price list pairs with it.
  const [withAnnual, setWithAnnual] = useState(true);
  const [discount, setDiscount] = useState(DEFAULT_ANNUAL_DISCOUNT);
  // The yearly price, worked out from the discount until someone types one (2026-10-21).
  const [annualPrice, setAnnualPrice] = useState("");
  const [annualTyped, setAnnualTyped] = useState(false);
  const [busy, setBusy] = useState(false);

  // The plans a new tier can start from: real tiers, never another workspace's bespoke deal.
  const bases = useMemo(
    () => plans.filter((p) => p.plan_class === "standard" || p.plan_class === "free"),
    [plans],
  );

  useEffect(() => {
    if (!open) return;
    setName(plan?.name ?? "");
    setPlanId(plan?.id ?? "");
    setIdTouched(false);
    setBasePlan(
      bases.find((p) => p.id === "launch")?.id ?? bases.find((p) => p.status === "active")?.id ?? "",
    );
    setBilling(plan?.interval ?? "month");
    setPrice(dollars(plan?.base_price_cents));
    setSeatPrice(plan && plan.seat_price_cents ? dollars(plan.seat_price_cents) : "");
    setCredits(plan ? String(plan.included_credits) : "");
    setMaxSeats(plan?.max_seats != null ? String(plan.max_seats) : "");
    setTrialDays(plan ? String(plan.trial_days) : "0");
    setDescription(plan?.description ?? "");
    setWithAnnual(true);
    setDiscount(DEFAULT_ANNUAL_DISCOUNT);
    setAnnualPrice("");
    setAnnualTyped(false);
  }, [open, plan, bases]);

  const effectiveId = creating ? (idTouched ? slug(planId) : slug(name)) : plan?.id ?? "";
  const reserved =
    creating && (RESERVED_IDS.includes(effectiveId) || effectiveId.startsWith("custom-"));
  const taken = creating && plans.some((p) => p.id === effectiveId);
  const priceCents = cents(price);
  const seatCents = seatPrice.trim() === "" ? null : cents(seatPrice);
  const creditCount = whole(credits);
  const seatLimit = maxSeats.trim() === "" ? null : whole(maxSeats);
  const trial = whole(trialDays);
  const makeAnnual = creating && billing === "month" && withAnnual;
  // A typed yearly price sets the discount it comes to; otherwise the discount sets the price.
  const typedAnnual = annualTyped ? cents(annualPrice) : null;
  const shownDiscount =
    typedAnnual != null && priceCents != null && priceCents > 0 && typedAnnual <= priceCents * 12
      ? String(impliedPct(priceCents, typedAnnual))
      : discount;
  const discountPct = whole(shownDiscount);
  const derivedAnnual =
    priceCents != null && discountPct != null && discountPct <= MAX_ANNUAL_DISCOUNT
      ? annualCents(priceCents, discountPct)
      : null;
  const annualPriceCents = annualTyped ? typedAnnual : derivedAnnual;
  const annualId = `${effectiveId}-annual`;

  const errors = {
    name: name.trim() ? undefined : "Give the plan a name.",
    id: !creating
      ? undefined
      : !effectiveId
        ? "An id is needed."
        : reserved
          ? "That id is reserved. Choose another."
          : taken
            ? "A plan with this id already exists."
            : undefined,
    base: creating && !basePlan ? "Choose a plan to copy what is included from." : undefined,
    price: priceCents == null ? "Enter a price of 0 or more." : undefined,
    seatPrice: seatPrice.trim() !== "" && seatCents == null ? "Enter 0 or more, or leave blank." : undefined,
    credits: creditCount == null ? "Enter a whole number of credits." : undefined,
    maxSeats: maxSeats.trim() !== "" && seatLimit == null ? "A whole number, or blank for no limit." : undefined,
    trial: trial == null ? "A whole number of days." : undefined,
    discount: !makeAnnual
      ? undefined
      : !annualTyped && (discountPct == null || discountPct > MAX_ANNUAL_DISCOUNT)
        ? `A whole number from 0 to ${MAX_ANNUAL_DISCOUNT}.`
        : plans.some((p) => p.id === annualId)
          ? `${annualId} already exists. Untick this, or choose another plan id.`
          : undefined,
    annualPrice: !makeAnnual || !annualTyped
      ? undefined
      : typedAnnual == null
        ? "Enter the yearly price, 0 or more."
        : priceCents != null && typedAnnual > priceCents * 12
          ? "More than twelve monthly payments, so nobody would choose annual."
          : discountPct != null && discountPct > MAX_ANNUAL_DISCOUNT
            ? `More than ${MAX_ANNUAL_DISCOUNT}% off twelve monthly payments.`
            : undefined,
  };
  const valid = Object.values(errors).every((e) => e === undefined);

  async function save() {
    if (!valid || priceCents == null || creditCount == null || trial == null) return;
    setBusy(true);
    try {
      if (creating) {
        const result = await api.createPlan({
          plan_id: effectiveId,
          name: name.trim(),
          base_plan_id: basePlan,
          base_price_cents: priceCents,
          included_credits: creditCount,
          description: description.trim(),
          seat_price_cents: seatCents,
          interval: billing,
          max_seats: seatLimit,
          trial_days: trial,
          annual_discount_pct: makeAnnual ? discountPct : null,
          annual_price_cents: makeAnnual && annualTyped ? typedAnnual : null,
        });
        toast.success(
          result.annual
            ? `${name.trim()} and ${name.trim()} (annual) saved as drafts`
            : `${name.trim()} saved as a draft`,
          result.annual
            ? "Neither is on the price list yet. Check what they include, then publish both from the Plans list."
            : "It is not on the price list yet. Check what it includes, then publish it from the Plans list.",
        );
        if (result.warning) toast.error("Check the price", result.warning);
        else if (result.annual?.warning) toast.error("Check the annual price", result.annual.warning);
      } else if (plan) {
        // Only what changed, so a field nobody touched is never rewritten.
        const body: Record<string, unknown> = {};
        if (name.trim() !== plan.name) body.name = name.trim();
        if (description.trim() !== (plan.description ?? "")) body.description = description.trim();
        if (priceCents !== plan.base_price_cents) body.base_price_cents = priceCents;
        if ((seatCents ?? 0) !== (plan.seat_price_cents ?? 0)) body.seat_price_cents = seatCents ?? 0;
        if (creditCount !== plan.included_credits) body.included_credits = creditCount;
        if (seatLimit !== plan.max_seats) body.max_seats = seatLimit;
        if (trial !== plan.trial_days) body.trial_days = trial;
        if (Object.keys(body).length === 0) {
          onClose();
          return;
        }
        await api.updatePlan(plan.id, body);
        toast.success(
          `${name.trim()} updated`,
          body.base_price_cents !== undefined
            ? "Credits and limits apply now. The new price applies to new purchases; customers already paying through Stripe keep their current price."
            : "Applies to every subscription on it except grandfathered ones.",
        );
      }
      onDone();
      onClose();
    } catch (err) {
      toast.error(
        creating ? "Couldn't create the plan" : "Couldn't update the plan",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={creating ? "New plan" : `Edit ${plan?.name ?? "plan"}`}
      description={
        creating
          ? "Starts as a draft with what another plan includes. Nothing reaches the price list until you publish it."
          : "Credits and limits apply to every subscription on this plan except grandfathered ones. A new price applies to new purchases: customers already paying through Stripe keep the price they signed up at."
      }
    >
      <div className={styles.form}>
        <div className={styles.row}>
          <Field label="Name" error={name !== "" ? errors.name : undefined}>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Scale" />
          </Field>
          {creating ? (
            <Field
              label="Plan id"
              hint={effectiveId ? `Saved as ${effectiveId}` : "Used in URLs and invoices; cannot change later."}
              error={name !== "" || idTouched ? errors.id : undefined}
            >
              <Input
                value={idTouched ? planId : effectiveId}
                onChange={(e) => {
                  setIdTouched(true);
                  setPlanId(e.target.value);
                }}
                placeholder="scale"
              />
            </Field>
          ) : (
            <Field label="Plan id" hint="Fixed: existing subscriptions point at it.">
              <Input value={plan?.id ?? ""} disabled readOnly />
            </Field>
          )}
        </div>

        {creating && (
          <div className={styles.row}>
            <Field
              label="Copy what is included from"
              hint="Edit the details afterwards with What's included."
              error={errors.base}
            >
              <Select
                value={basePlan}
                onChange={(e) => setBasePlan(e.target.value)}
                options={bases.map((p) => ({ value: p.id, label: `${p.name} (${p.id})` }))}
              />
            </Field>
            <Field label="Billed">
              <Select
                value={billing}
                onChange={(e) => setBilling(e.target.value)}
                options={[
                  { value: "month", label: "Monthly" },
                  { value: "year", label: "Yearly" },
                ]}
              />
            </Field>
          </div>
        )}

        <div className={styles.row}>
          <Field
            label={`Price per ${(creating ? billing : plan?.interval) === "year" ? "year" : "month"} (${plan?.currency ?? "USD"})`}
            error={price !== "" ? errors.price : undefined}
          >
            <Input type="number" min="0" step="1" value={price}
              onChange={(e) => setPrice(e.target.value)} placeholder="149" />
          </Field>
          <Field label="Included credits" error={credits !== "" ? errors.credits : undefined}>
            <Input type="number" min="0" step="100" value={credits}
              onChange={(e) => setCredits(e.target.value)} placeholder="4000" />
          </Field>
        </div>
        {twin && (
          // Reported 2026-10-09 as "a plan changed in Admin does not reach the billing page": a
          // monthly price was edited while customers see the tier's ANNUAL plan first. The two are
          // separate plans and neither follows the other.
          <p className={styles.hint}>
            {twin.name} is this tier billed {twin.interval === "year" ? "yearly" : "monthly"}, a
            separate plan at {formatPrice(twin.base_price_cents, twin.currency)}
            {twin.interval === "year" ? " a year" : " a month"}. Customers see the annual price
            first, so edit both when the tier&rsquo;s price changes.
          </p>
        )}

        {creating && billing === "month" && (
          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>Annual plan</legend>
            <label className={styles.checkRow}>
              <Checkbox
                label="Also create the annual plan"
                checked={withAnnual}
                onChange={(e) => setWithAnnual(e.target.checked)}
              />
              <span>Also sell it yearly, as {annualId}</span>
            </label>
            {withAnnual && (
              <>
                <div className={styles.row}>
                  <Field
                    label="Annual discount (%)"
                    hint="Off twelve monthly payments."
                    error={errors.discount}
                  >
                    <Input type="number" min="0" max={MAX_ANNUAL_DISCOUNT} step="1"
                      value={shownDiscount}
                      onChange={(e) => {
                        setDiscount(e.target.value);
                        setAnnualTyped(false);
                      }} />
                  </Field>
                  <Field
                    label="Annual price per year (USD)"
                    hint={annualTyped
                      ? "Set by you. Change the discount to work it out again."
                      : "Worked out from the discount. Type a price to set it yourself."}
                    error={errors.annualPrice}
                  >
                    <Input type="number" min="0" step="1"
                      value={annualTyped ? annualPrice : dollars(derivedAnnual)}
                      onChange={(e) => {
                        setAnnualTyped(true);
                        setAnnualPrice(e.target.value);
                      }}
                      placeholder="950" />
                  </Field>
                </div>
                <p className={styles.hint} aria-live="polite">
                  {priceCents != null && annualPriceCents != null && errors.annualPrice === undefined
                    && discountPct != null && discountPct <= MAX_ANNUAL_DISCOUNT
                    ? `${name.trim() || "The plan"} (annual): ${formatPrice(annualPriceCents, "USD")} a year instead of ${formatPrice(priceCents * 12, "USD")}, ${discountPct}% off${creditCount != null ? `, ${(creditCount * 12).toLocaleString()} credits a year` : ""}. Credits stay twelve months' worth. Created as a draft beside the monthly plan; the price list shows them as one card.`
                    : "Enter the monthly price to see the annual price."}
                </p>
              </>
            )}
          </fieldset>
        )}

        <div className={styles.row}>
          <Field label="Price per extra seat" hint="Blank for none." error={errors.seatPrice}>
            <Input type="number" min="0" step="1" value={seatPrice}
              onChange={(e) => setSeatPrice(e.target.value)} placeholder="" />
          </Field>
          <Field label="Seat limit" hint="Blank for no limit." error={errors.maxSeats}>
            <Input type="number" min="0" step="1" value={maxSeats}
              onChange={(e) => setMaxSeats(e.target.value)} />
          </Field>
          <Field label="Trial days" error={errors.trial}>
            <Input type="number" min="0" step="1" value={trialDays}
              onChange={(e) => setTrialDays(e.target.value)} />
          </Field>
        </div>

        <Field label="Description" hint="Shown on the price list.">
          <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)}
            placeholder="For teams running several territories." />
        </Field>

        <div className={styles.actions}>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!valid}>
            {creating ? "Create draft plan" : "Save changes"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
