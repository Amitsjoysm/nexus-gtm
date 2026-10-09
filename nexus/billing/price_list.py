# nexus/billing/price_list.py
"""Which plans the customer price list shows, and the four-card limit (asked 2026-10-09).

`GET /billing/plans` listed every active standard and free plan. A superadmin now shows or hides
each one, and at most ``MAX_CARDS`` cards are shown. A card is a tier the way PlanPicker draws it:
a plan sold monthly and annually (``plans.interval_pairs``) is one card, so the seeded ladder (Free,
Launch, Accelerate) is three.

Hiding is presentation, not a hold. A hidden plan stays on sale: existing subscribers keep it, a
console Checkout link or a plan change can still use it, and checkout by id still works.
``status="draft"`` is how a plan stops being sold. The flag lives in ``plan.meta`` and its absence
means shown, so no plan moved when this shipped, and ``sync_plans`` (which never mutates a row)
cannot reset a superadmin's choice.
"""
from __future__ import annotations

from nexus.billing.plans import interval_pairs

MAX_CARDS = 4
LISTABLE_CLASSES = ("standard", "free")
_HIDDEN = "price_list_hidden"


class PriceListError(Exception):
    """A request the price list refuses. ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_shown(plan) -> bool:
    return not (plan.meta or {}).get(_HIDDEN)


def listable(plan) -> bool:
    """Classes the price list can carry at all. Enterprise and custom deals are agreed, trial is
    entered by signing up, internal and unlimited are not for sale."""
    return plan.plan_class in LISTABLE_CLASSES


def on_price_list(plan) -> bool:
    return plan.status == "active" and listable(plan) and is_shown(plan)


def families(plans) -> dict[str, str]:
    """Plan id -> its card, for every plan on the price list. Pairing runs over the listed rows
    only, as `GET /billing/plans` does, so hiding a monthly plan leaves its annual a card of its
    own: splitting a pair cannot sneak a fifth card past the limit."""
    rows = [p for p in plans if on_price_list(p)]
    pairs = interval_pairs(rows)
    return {p.id: pairs[p.id].family for p in rows}


def card_count(plans) -> int:
    return len(set(families(plans).values()))


def _mark(plan, shown: bool) -> None:
    meta = dict(plan.meta or {})
    if shown:
        meta.pop(_HIDDEN, None)
    else:
        meta[_HIDDEN] = True
    plan.meta = meta


async def _listable_rows(session) -> list:
    from sqlalchemy import select

    from nexus.models.billing import BillingPlan

    return list((await session.scalars(
        select(BillingPlan).where(BillingPlan.plan_class.in_(LISTABLE_CLASSES),
                                  BillingPlan.status == "active")
    )).all())


async def set_shown(session, plan, shown: bool) -> None:
    """Show or hide ``plan``. Showing one that would make a fifth card is refused, and nothing
    changes. A draft can be hidden ahead of publishing; it counts once it is on sale."""
    if not listable(plan):
        raise PriceListError(
            f"'{plan.name}' is a {plan.plan_class} plan, which is never on the price list.", 400)
    if shown == is_shown(plan):
        return
    _mark(plan, shown)
    if shown and plan.status == "active" and card_count(await _listable_rows(session)) > MAX_CARDS:
        _mark(plan, False)
        raise PriceListError(
            f"At most {MAX_CARDS} plans can be shown on the price list (a plan sold monthly and "
            "annually counts once). Hide one first.")


async def fit_after_publish(session, plan) -> str:
    """Keep a plan that just went on sale on the price list if it fits; otherwise hide it and
    return the sentence that says so. Publishing is never refused for this: a plan can be on sale
    and off the list."""
    if not on_price_list(plan) or card_count(await _listable_rows(session)) <= MAX_CARDS:
        return ""
    _mark(plan, False)
    return (f"It is on sale but not on the price list: {MAX_CARDS} plans are already shown. "
            "Hide one to show it.")
