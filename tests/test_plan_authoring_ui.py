"""The Superadmin Plans tab can create a plan, edit its terms, and publish or hold it.

Reported 2026-10-01 as "I can view plans and subscriptions but cannot edit or add a plan". Nothing
had broken: `POST /admin/billing/plans`, `PATCH /plans/{id}` and `PUT /plans/{id}/status` shipped on
2026-08-25 with no screen calling any of them (the API client even carried an unused `updatePlan`),
and the tab offered only "What's included". The plan change for a workspace did exist, behind a
button labelled "Manage". There is no frontend test runner, so these read the source.
"""
from __future__ import annotations

from pathlib import Path

PAGE = Path("frontend/src/pages/AdminBillingPage.tsx")
DIALOG = Path("frontend/src/pages/admin/PlanDialog.tsx")
API = Path("frontend/src/lib/api.ts")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_the_plans_tab_offers_new_plan_edit_and_publish():
    page = _read(PAGE)
    assert "New plan" in page and "<PlanDialog" in page
    assert "setPlanForm(r)" in page, "each plan row can be edited"
    assert "api.setPlanStatus" in page and '"Publish"' in page and '"Hold"' in page


def test_every_plan_control_is_behind_the_permission_the_server_checks():
    """The server refuses New plan / Edit / Publish / Hold without `stripe.manage` (superadmin only
    since 2026-10-09: a plan on sale lives in Stripe), and "What's included" without
    `pricing.write`. Offering either to someone without it would be a door that 403s."""
    page = _read(PAGE)
    plans = page[page.index("function Plans()"):page.index("function Subscriptions()")]
    assert "const canEdit = can(PRICING_WRITE);" in plans
    assert "const canManageStripe = can(STRIPE_MANAGE);" in plans
    assert "{canManageStripe && (" in plans        # the New plan toolbar
    assert "...(canEdit || canManageStripe" in plans  # the row actions column


def test_publish_and_hold_only_where_the_server_allows_it():
    page = _read(PAGE)
    assert 'r.plan_class === "standard"' in page
    assert 'r.status === "draft" || r.status === "active"' in page


def test_the_dialog_creates_drafts_and_never_patches_a_status():
    """Status goes through PUT .../status, which refuses to publish a non-standard plan; the PATCH
    body would accept a status and skip that rule."""
    dialog = _read(DIALOG)
    assert "api.createPlan(" in dialog and "api.updatePlan(" in dialog
    assert "body.status" not in dialog and "status:" not in dialog.split("api.updatePlan")[0][-400:]
    assert "base_plan_id: basePlan" in dialog, "a new plan clones what another plan includes"
    for reserved in ("free", "trial", "enterprise", "internal", "legacy-unlimited"):
        assert f'"{reserved}"' in dialog


def test_the_api_client_has_each_call_once():
    api = _read(API)
    for method in ("createPlan(", "updatePlan(", "setPlanStatus("):
        assert api.count(f"  {method}") == 1, method


def test_the_subscription_plan_change_is_named_for_what_it_does():
    page = _read(PAGE)
    assert '{canCustomPlan ? "Change plan" : "Manage"}' in page
