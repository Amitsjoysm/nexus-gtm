"""The billing screens say what is true at every step of the Stripe flow (2026-10-23).

There is no frontend test runner, so these read the source, like `test_plan_picker_ui.py`. Each
pins one thing a customer met walking the flow on the local deploy.
"""
from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"
PICKER = (FRONTEND / "pages" / "billing" / "PlanPicker.tsx").read_text(encoding="utf-8")
PAGE = (FRONTEND / "pages" / "BillingPage.tsx").read_text(encoding="utf-8")
TYPES = (FRONTEND / "lib" / "types.ts").read_text(encoding="utf-8")


def test_the_price_list_does_not_promise_proration():
    # Checkout charges the full price, an upgrade charges the full price now, and a downgrade waits
    # for the period to end: nothing is "charged for the days you use".
    assert "charged for the days you use" not in PICKER


def test_manage_payment_method_appears_only_with_a_payment_account():
    # A Free workspace pressing it got "This workspace has no payment account yet".
    assert "payment_account" in PICKER and "payment_account" in TYPES


def test_a_plan_set_to_end_can_be_kept_here():
    # Resuming was possible only on Stripe's page; the price list now offers Keep for both kinds
    # of hold, a scheduled downgrade and a plan set to end.
    assert "endsWithPeriod" in PICKER
    keep = PICKER[PICKER.index("{held && ("):PICKER.index("{families.length > 0")]
    assert "keepPlan" in keep and "scheduled &&" not in keep.split("<Button")[0][-40:]


def test_returning_from_checkout_is_acknowledged():
    # The page ignored ?checkout=success, so a customer whose webhook was a moment behind saw Free
    # and no word that the payment went through; ?checkout=cancelled said nothing either.
    assert '"checkout"' in PAGE
    assert "success" in PAGE and "cancelled" in PAGE
    assert "replaceState" in PAGE   # the parameter is consumed, so a refresh does not repeat it


def test_an_invoice_links_to_stripes_copy_and_names_its_period():
    assert "hosted_url" in TYPES and "pdf_url" in TYPES and "issued_at" in TYPES
    assert "hosted_url" in PAGE and "pdf_url" in PAGE
    # The period of a Stripe invoice is its service period, never the `stripe:in_...` key.
    assert "period_start" in PAGE and 'startsWith("stripe:")' not in PAGE.split("function periodLabel")[0]


def test_a_plan_set_to_end_reads_ends_not_renews():
    assert "cancel_at_period_end" in PAGE and '"Ends"' in PAGE
