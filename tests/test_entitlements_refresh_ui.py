"""The menu's padlocks follow the workspace and the plan the person is on NOW (2026-11-04).

Reported as "Mail and Calls show a padlock even on plans below the top one". The server locked
nothing on Launch: the client read the plan gates once per page load, outside the part of the shell
that remounts on a workspace switch, and nothing re-read them after a plan change. So the padlocks
were those of the previous workspace (a Free one, measured: switching workspace sent no request),
or of the plan before an in-app upgrade or a Checkout whose webhook landed after the page loaded.
There is no frontend test runner, so these read the source, as the other `_ui` suites do.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_switching_workspace_reads_the_new_workspaces_gates():
    shell = _read("components/layout/AppShell.tsx")
    assert re.search(r"<EntitlementsProvider\s+key=\{tenantEpoch\}>", shell), \
        "the plan gates are not re-read when the workspace changes"


def test_a_plan_change_in_the_app_reads_the_gates_again():
    picker = _read("pages/billing/PlanPicker.tsx")
    switch = picker[picker.index("async function switchTo"):]
    switch = switch[:switch.index("finally")]
    assert "refreshEntitlements()" in switch, "an in-app upgrade leaves the old plan's padlocks"


def test_returning_from_checkout_reads_the_gates_once_stripe_confirms():
    page = _read("pages/BillingPage.tsx")
    confirmed = page[page.index("if (now.switch_in_place)"):]
    confirmed = confirmed[:confirmed.index("return;")]
    assert "refreshEntitlements()" in confirmed, "a customer who just paid still sees Free's locks"


def test_coming_back_to_the_tab_rereads_them_but_not_on_every_glance():
    ctx = _read("app/EntitlementsContext.tsx")
    assert "visibilitychange" in ctx and "REREAD_ON_RETURN_MS" in ctx
    assert "export function useRefreshEntitlements" in ctx
