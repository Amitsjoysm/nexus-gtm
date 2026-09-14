# tests/test_plan_picker_ui.py
"""The Billing page's plan picker: one card per tier, annual by default, numbers from the data.

It used to render one card per plan ROW, so "Launch" and "Launch (annual)" sat side by side as if
they were two products. It is now one card per tier behind an Annual | Monthly switch.

There is no frontend test runner here, so these read the source, the same approach as
`test_alert_setup_ui.py` and the nav/route guard tests. What they pin are the places where the
picker could quietly drift back: rendering rows instead of tiers, guessing pairs in the browser,
buying a different plan than the one on screen, or writing a saving down as a literal that stops
being true the day Admin reprices.
"""
from __future__ import annotations

import pathlib
import re

FRONTEND = pathlib.Path("frontend/src")
PICKER = FRONTEND / "pages/billing/PlanPicker.tsx"
FAMILIES = FRONTEND / "pages/billing/planFamilies.ts"
TYPES = FRONTEND / "lib/types.ts"


def _read(path: pathlib.Path) -> str:
    assert path.exists(), f"{path} is missing — was it moved?"
    return path.read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    match = re.search(rf"export function {name}\(.*?\n\}}", src, re.S)
    assert match, f"{name} not found — was it renamed?"
    return match.group(0)


def test_the_client_type_carries_exactly_the_fields_the_server_sends():
    """`family` and `counterpart_id` are what let the picker group without guessing. A client type
    missing one would compile against a response that no longer matches it."""
    from nexus.api.routers.billing import SellablePlanOut

    block = re.search(r"export interface SellablePlan \{(.*?)\n\}", _read(TYPES), re.S)
    assert block, "SellablePlan not found in types.ts"
    client_fields = set(re.findall(r"^\s{2}([a-z_]+):", block.group(1), re.M))
    assert client_fields == set(SellablePlanOut.model_fields), (
        f"client {sorted(client_fields)} vs server {sorted(SellablePlanOut.model_fields)}"
    )


def test_the_default_interval_is_annual():
    """What was asked for. The one exception (a workspace already paying for a tier sold both ways
    opens on the interval it pays) returns early; everyone else falls through to annual."""
    body = _function(_read(FAMILIES), "defaultInterval")
    returns = re.findall(r'return "(year|month)";', body)
    assert returns and returns[-1] == "year", f"defaultInterval falls through to {returns[-1:]}"

    picker = _read(PICKER)
    assert "chosen ?? defaultInterval(families)" in picker, (
        "the picker no longer derives its opening interval from defaultInterval"
    )
    assert 'useState<BillingInterval>("month")' not in picker


def test_the_switch_lists_annual_first():
    block = re.search(r"const INTERVALS[^=]*=\s*\[(.*?)\];", _read(PICKER), re.S)
    assert block, "INTERVALS not found — was it renamed?"
    assert re.findall(r'value: "(year|month)"', block.group(1)) == ["year", "month"]


def test_one_card_per_family_not_one_per_plan():
    picker = _read(PICKER)
    assert "groupFamilies(" in picker and "families.map(" in picker
    assert "plans.data.map(" not in picker, "the picker renders plan rows again, not tiers"


def test_the_browser_groups_on_the_server_family_and_never_on_ids_or_names():
    """The pairing rule lives in `interval_pairs`, where it is tested against authored plans. A
    second copy here would be the one nobody updates."""
    grouping = _function(_read(FAMILIES), "groupFamilies")
    assert "plan.family" in grouping
    for leak in ("-annual", ".name", ".id", "counterpart_id"):
        assert leak not in grouping, f"groupFamilies reads {leak!r}"
    assert "-annual" not in _read(PICKER)


def test_checkout_buys_the_plan_shown_for_the_selected_interval():
    picker = _read(PICKER)
    assert "const plan = shownPlan(family, interval);" in picker
    assert "onCheckout(plan.id)" in picker, "the checkout button is not bound to the shown plan"

    shown = _function(_read(FAMILIES), "shownPlan")
    assert re.search(r'interval === "year"\s*\?\s*\(family\.annual', shown), (
        "shownPlan no longer prefers the annual row when the switch says annual"
    )


def test_the_benefits_are_computed_from_plan_fields():
    body = _function(_read(FAMILIES), "annualBenefits")
    for field in ("base_price_cents * 12", "max_seats", "included_credits"):
        assert field in body, f"annualBenefits no longer uses {field}"


def test_no_price_or_saving_is_written_down():
    """A literal saving is true until Admin reprices, and then it is a false claim on a payment
    screen. The seed's "two months free" understated the real saving from day one."""
    for path in (PICKER, FAMILIES):
        src = _read(path)
        assert not re.search(r"\$\d", src), f"{path} contains a dollar literal"
        # A quoted CSS length (`width="100%"`) is layout, not a claim.
        assert not re.search(r'(?<![\w"])\d+%(?!")', src), f"{path} contains a percentage literal"
        assert "months free" not in src.lower(), f"{path} repeats the seed's 'months free'"
        for amount in ("238", "478", "950", "1910", "1,910", "79.17", "159.17"):
            assert amount not in src, f"{path} hard-codes {amount}"


def test_the_switch_is_a_keyboard_operable_radio_group():
    picker = _read(PICKER)
    for needle in ('role="radiogroup"', 'role="radio"', "aria-checked={checked}",
                   "tabIndex={checked ? 0 : -1}", '"ArrowRight"', '"ArrowLeft"'):
        assert needle in picker, f"the interval switch lost {needle}"


def test_the_price_change_respects_reduced_motion():
    assert "useReducedMotion()" in _read(PICKER)


def test_the_paths_that_must_not_change_survive():
    """The admin-managed contract message, the portal button, and the checkout wording: the plan
    changes by webhook, not on click, so the button must not claim it upgraded anything."""
    picker = _read(PICKER)
    assert 'usage?.plan_class === "custom"' in picker
    assert "Manage payment method" in picker
    assert "Continue to checkout" in picker
