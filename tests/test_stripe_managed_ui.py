# tests/test_stripe_managed_ui.py
"""The console offers the Stripe controls only to holders of `stripe.manage` (no frontend test
runner, so these read the source, like the other *_ui tests)."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_plan_authoring_is_gated_on_stripe_manage():
    page = _read("pages/AdminBillingPage.tsx")
    assert "const canManageStripe = can(STRIPE_MANAGE);" in page
    assert "{canManageStripe && (" in page              # New plan / Sync toolbar
    assert "Sync to Stripe" in page
    assert "api.syncPlansToStripe()" in page


def test_the_dialog_starts_and_moves_stripe_subscriptions_for_superadmins_only():
    dialog = _read("pages/admin/TenantActionsDialog.tsx")
    assert "STRIPE_MANAGE" in dialog
    assert "Start a Stripe subscription" in dialog
    assert "api.startStripeSubscription(" in dialog
    assert "tenant?.stripe_billed" in dialog or "tenant.stripe_billed" in dialog
    assert "Email an invoice" in dialog and "Checkout link" in dialog


def test_the_client_and_types_carry_the_new_calls():
    api = _read("lib/api.ts")
    types = _read("lib/types.ts")
    assert "syncPlansToStripe()" in api and "/admin/billing/plans/stripe-sync" in api
    assert "startStripeSubscription(" in api and "/stripe-subscription" in api
    assert "stripe_billed: boolean;" in types
    assert "stripe_price_id: string;" in types


def test_activation_shows_the_sync_report():
    assert "stripe_sync" in _read("pages/admin/PaymentsTab.tsx")


def test_new_copy_uses_no_em_dashes():
    """Console copy rule: commas, colons and parentheses, never an em dash, in the strings this
    change adds."""
    dialog = _read("pages/admin/TenantActionsDialog.tsx")
    start = dialog.index("Start a Stripe subscription")
    block = dialog[start:start + 4000]
    assert "—" not in block


def test_the_payments_tab_registers_the_webhook_for_superadmins_only():
    tab = _read("pages/admin/PaymentsTab.tsx")
    assert "const canManageStripe = usePlatformCan()(STRIPE_MANAGE);" in tab
    assert "{canManageStripe && (" in tab
    assert "Create webhook in Stripe" in tab
    assert "api.registerStripeWebhook(" in tab
    assert "/webhook-endpoint" in _read("lib/api.ts")


def test_the_secret_field_tells_a_superadmin_it_can_be_left_blank():
    tab = _read("pages/admin/PaymentsTab.tsx")
    assert "Leave blank and press Create webhook in Stripe" in tab
