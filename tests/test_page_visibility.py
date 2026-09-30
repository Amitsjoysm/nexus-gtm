"""Hide a page from every workspace: out of the menu, and its address sends people to Dashboard.

Asked for by the product owner 2026-09-29 ("I don't want to show the Approvals page or the Network
page"), per PAGE rather than per module: Approvals shares `module.agents` with Orchestrator and AI
Runs, and hiding the module would have taken all three. Hiding is presentation plus the route; the
feature behind a page keeps running, so an approval can still be decided from its run's page.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import auth, signup

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"

#: Pages that can never be hidden. The first five are the product's floor; Billing is where a
#: customer fixes the plan; the Superadmin console is where a hide is undone.
NEVER_HIDDEN = {"/dashboard", "/accounts", "/contacts", "/members", "/settings",
                "/settings/billing", "/admin/billing", "/admin/health"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    from nexus.features import pages

    pages.invalidate()
    yield
    pages.invalidate()


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


def _nav_paths() -> set[str]:
    nav = (SRC / "app" / "nav.tsx").read_text(encoding="utf-8")
    items = nav[nav.index("export const NAV_ITEMS"):nav.index("export function canSee")]
    return set(re.findall(r'to: "([^"]+)"', items))


# ---- the catalog ----------------------------------------------------------------------------------

def test_every_menu_page_is_hideable_except_the_floor():
    """The catalog and the menu are pinned together, so a page added to the menu later cannot be
    left out of the switch, and the floor can never be put in it."""
    from nexus.features.pages import HIDEABLE_PAGES

    hideable = {p.path for p in HIDEABLE_PAGES}
    assert not hideable & NEVER_HIDDEN
    assert hideable == _nav_paths() - NEVER_HIDDEN
    assert len({p.key for p in HIDEABLE_PAGES}) == len(HIDEABLE_PAGES)


# ---- the switch -----------------------------------------------------------------------------------

async def test_a_hidden_page_reaches_every_workspace_and_can_be_shown_again(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()
    boss = await _superadmin(client, monkeypatch, slug="pv1", email="boss@pv1.com")
    other = await signup(client, slug="pv1b", email="o@pv1b.com", company="Other")

    listed = (await client.get("/api/admin/features/pages", headers=auth(boss))).json()
    approvals = next(p for p in listed["pages"] if p["key"] == "approvals")
    assert approvals == {**approvals, "path": "/approvals", "hidden": False,
                         "module": "module.agents"}

    r = await client.put("/api/admin/features/pages/approvals", headers=auth(boss),
                         json={"hidden": True})
    assert r.status_code == 200 and r.json()["hidden"] is True
    seen = (await client.get("/api/billing/entitlements", headers=auth(other))).json()
    assert seen["hidden_pages"] == ["/approvals"]
    # Hiding a page does not switch its module off: Orchestrator and AI Runs are untouched.
    agents = next(m for m in seen["modules"] if m["capability_id"] == "module.agents")
    assert agents["locked"] is False

    await client.put("/api/admin/features/pages/approvals", headers=auth(boss),
                     json={"hidden": False})
    seen = (await client.get("/api/billing/entitlements", headers=auth(other))).json()
    assert seen["hidden_pages"] == []


async def test_the_floor_cannot_be_hidden(client, monkeypatch):
    boss = await _superadmin(client, monkeypatch, slug="pv2", email="boss@pv2.com")
    for key in ("dashboard", "accounts", "billing", "admin"):
        r = await client.put(f"/api/admin/features/pages/{key}", headers=auth(boss),
                             json={"hidden": True})
        assert r.status_code == 400, key


async def test_hiding_is_audited(client, monkeypatch):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    boss = await _superadmin(client, monkeypatch, slug="pv3", email="boss@pv3.com")
    await client.put("/api/admin/features/pages/network", headers=auth(boss),
                     json={"hidden": True, "note": "not sold yet"})
    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(select(BillingAuditLog).where(
            BillingAuditLog.action == "feature.page_visibility"))).all()
    assert len(rows) == 1
    assert rows[0].target == "network" and rows[0].after == {"hidden": True}


async def test_a_workspace_owner_cannot_hide_pages(client):
    token = await signup(client, slug="pv4", email="o@pv4.com", company="PV4")
    r = await client.put("/api/admin/features/pages/network", headers=auth(token),
                         json={"hidden": True})
    assert r.status_code == 404
    assert (await client.get("/api/admin/features/pages", headers=auth(token))).status_code == 404


async def test_an_unreadable_setting_hides_nothing(monkeypatch):
    """A hide is a restriction; failing to read one applies none, like every switch here."""
    from nexus.features import pages

    async def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr(pages, "_load", boom)
    pages.invalidate()
    assert await pages.hidden_paths() == []


# ---- the screens ----------------------------------------------------------------------------------

def test_the_menu_leaves_out_hidden_pages():
    sidebar = (SRC / "components" / "layout" / "Sidebar.tsx").read_text(encoding="utf-8")
    assert "isPageHidden(entitlements, item.to)" in sidebar


def test_every_route_of_a_hideable_page_redirects_when_hidden():
    """Sub-pages too: hiding AI Runs must also close /runs/:id, or a bookmark still opens it."""
    from nexus.features.pages import HIDEABLE_PAGES

    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    routes = re.findall(r'<Route\s+path="(/[^"]*)"\s+element=\{(.*?)\}\s*/>', app, re.S)
    assert routes
    for page in HIDEABLE_PAGES:
        mine = [(path, element) for path, element in routes
                if path == page.path or path.startswith(page.path + "/")]
        assert mine, f"no route for {page.path}"
        for path, element in mine:
            assert f'<RequirePage page="{page.path}"' in element, f"{path} ignores a hidden page"
    guard = (SRC / "app" / "RequirePage.tsx").read_text(encoding="utf-8")
    assert '<Navigate to="/dashboard" replace />' in guard


def test_the_feature_switches_tab_offers_the_pages():
    tab = (SRC / "pages" / "admin" / "FeatureSwitchesTab.tsx").read_text(encoding="utf-8")
    assert "api.adminPages(" in tab and "api.setPageHidden(" in tab
