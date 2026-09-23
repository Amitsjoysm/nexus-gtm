"""The engagement screens, checked by reading their source (spec §9).

There is no frontend test runner here, so like `test_plan_gated_nav.py` these pin the structural
promises the screens make: which engine a nav item belongs to, that every new route waits for the
engine switch, that nothing sends without a click, and that long AI work shows `WorkingIndicator`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
NAV = SRC / "app" / "nav.tsx"
APP = SRC / "App.tsx"
PAGES = SRC / "pages" / "engagement"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _nav_block(route: str) -> str:
    match = re.search(r'\{[^{}]*to:\s*"' + re.escape(route) + r'"[^{}]*\}', _read(NAV), re.S)
    assert match, f"{route} is not in NAV_ITEMS"
    return match.group(0)


def test_the_new_screens_appear_only_when_the_engine_is_on():
    for route in ("/engagement/campaigns", "/engagement/replies", "/engagement/templates"):
        assert 'engine: "on"' in _nav_block(route), f"{route} must wait for the engine switch"
    # The old engine's pages leave the menu at the same moment, so a workspace never sees both.
    for route in ("/campaigns", "/cadences"):
        assert 'engine: "off"' in _nav_block(route), f"{route} must leave when the engine is on"


def test_can_see_reads_the_engine_and_unknown_keeps_the_old_pages():
    source = _read(NAV)
    assert 'item.engine === "on" && engineOn !== true' in source
    assert 'item.engine === "off" && engineOn === true' in source
    assert "canSee(item, role, isPlatformAdmin, engineOn)" in _read(
        SRC / "components" / "layout" / "Sidebar.tsx")


def test_every_engagement_route_waits_for_the_engine():
    app = _read(APP)
    routes = re.findall(r'<Route\s+path="(/engagement/[^"]*)"\s+element=\{(.*?)\}\s*/>', app, re.S)
    assert {r for r, _ in routes} >= {
        "/engagement/campaigns", "/engagement/campaigns/new", "/engagement/campaigns/:campaignId",
        "/engagement/replies", "/engagement/replies/settings", "/engagement/templates",
    }
    for route, element in routes:
        assert "<RequireEngine" in element, f"{route} renders without checking the engine switch"
        assert "RequireCapability" in element, f"{route} is not behind its module gate"


def test_an_unreadable_engine_status_reads_as_off():
    """The new pages 404 while the engine is dark, so a status we could not read must not offer
    them; the old pages work either way."""
    source = _read(SRC / "app" / "EngagementContext.tsx")
    assert "state.error ? { engine_on: false" in source


def test_a_reply_is_sent_only_by_the_send_button():
    """D22: the AI drafts, a person presses Send. One call site, inside the click handler."""
    desk = _read(PAGES / "ReplyDeskPage.tsx")
    assert desk.count("api.deskSend(") == 1
    send_handler = desk[desk.index("async function send()"):]
    assert send_handler.index("api.deskSend(") < send_handler.index("}, [")
    assert "onClick={send}" in desk


def test_long_ai_work_shows_the_working_indicator():
    for page, reason in (("ReviewQueue.tsx", "writing and rewriting drafts"),
                         ("ReplyDeskPage.tsx", "suggesting a reply")):
        assert "<WorkingIndicator" in _read(PAGES / page), f"{page}: {reason} needs WorkingIndicator"


def test_launch_is_held_while_the_balance_cannot_cover_the_worst_case():
    panel = _read(PAGES / "LaunchPanel.tsx")
    assert "const blocked = e.gate_applies && !e.covered;" in panel
    assert "disabled={blocked" in panel


def test_the_engagement_pages_use_tokens_not_inline_styles():
    offenders = [p.name for p in list(PAGES.glob("*.tsx"))
                 + list((SRC / "components" / "engagement").glob("*.tsx"))
                 + [SRC / "pages" / "settings" / "EngagementSettings.tsx"]
                 if "style={{" in _read(p)]
    assert not offenders, f"inline styles in {offenders}: use the CSS modules and tokens"


def test_the_account_page_offers_emails_only_with_the_engine_on():
    page = _read(SRC / "pages" / "AccountDetailPage.tsx")
    assert '...(engineOn ? [{ value: "emails", label: "Emails" }] : [])' in page
    assert "<AccountConversations accountId={id} />" in page
