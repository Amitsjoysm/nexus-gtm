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


def test_the_engagement_screens_are_the_only_campaign_screens():
    """Since the cutover (spec §13) the old Campaigns and Cadences are gone. Their addresses forward,
    because bookmarks and old links still arrive at them."""
    for route in ("/engagement/campaigns", "/engagement/replies", "/engagement/templates"):
        assert 'engine: "on"' in _nav_block(route), f"{route} must leave when the engine is off"
    nav = _read(NAV)
    assert 'to: "/campaigns"' not in nav and 'to: "/cadences"' not in nav
    app = _read(APP)
    assert '<Route path="/campaigns" element={<Navigate to="/engagement/campaigns" replace />} />' in app
    assert '<Route path="/cadences" element={<Navigate to="/engagement/templates" replace />} />' in app
    for gone in ("CampaignsPage.tsx", "CadencesPage.tsx"):
        assert not (SRC / "pages" / gone).exists(), f"pages/{gone} belongs to the old engine"


def test_the_engagement_pages_leave_the_menu_only_when_the_engine_is_confirmed_off():
    source = _read(NAV)
    assert 'item.engine === "on" && engineOn === false' in source
    assert '"off"' not in source.split("export function canSee", 1)[1].split("\n}\n", 1)[0]
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


def test_an_unreadable_engine_status_reads_as_on():
    """After the cutover these are the only campaign screens, so an unreadable status must not
    delete them; the server is the boundary and answers 404 if the engine really is off. Off is
    said in place, keeping the URL, since there is no other campaign screen to send anyone to."""
    source = _read(SRC / "app" / "EngagementContext.tsx")
    assert "state.error ? { engine_on: true" in source
    assert "<Navigate" not in source and "<FeatureUnavailable" in source


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
    """The one inline style allowed passes DATA to CSS as a custom property (a bar's length);
    every look comes from the CSS modules and tokens."""
    offenders = []
    for p in (list(PAGES.glob("*.tsx")) + list((SRC / "components" / "engagement").glob("*.tsx"))
              + [SRC / "pages" / "settings" / "EngagementSettings.tsx"]):
        for match in re.finditer(r"style=\{\{([^}]*)\}", _read(p)):
            if not match.group(1).strip().startswith('"--'):
                offenders.append(p.name)
    assert not offenders, f"inline styles in {offenders}: use the CSS modules and tokens"


def test_the_account_page_offers_emails_only_with_the_engine_on():
    page = _read(SRC / "pages" / "AccountDetailPage.tsx")
    assert '...(engineOn ? [{ value: "emails", label: "Emails" }] : [])' in page
    assert "<AccountConversations accountId={id} />" in page


# ---- reporting (phase 12) ------------------------------------------------------------------------

def test_results_appear_once_a_campaign_has_launched():
    page = _read(PAGES / "CampaignDetailPage.tsx")
    assert '...(settingUp ? [] : [{ value: "results", label: "Results" }])' in page
    assert "<ReportsPanel campaignId={c.id} />" in page


def test_today_is_on_the_dashboard_only_with_the_engine_on():
    assert "{engineOn && <TodayPlan />}" in _read(SRC / "pages" / "DashboardPage.tsx")


def test_the_funnel_is_one_hue_with_bounces_beside_it_not_in_it():
    """One series, so one colour and no legend; a bounce is a problem, not a funnel stage."""
    panel = _read(PAGES / "ReportsPanel.tsx")
    funnel = panel[panel.index("const funnel"):panel.index("const widest")]
    assert "bounced" not in funnel.lower()
    assert "styles.bounced" in panel and "AlertTriangleIcon" in panel
    css = _read(PAGES / "ReportsPanel.module.css")
    assert css.count("background: var(--accent)") == 1


def test_my_mailboxes_shows_this_weeks_bounce_rate():
    assert "<MailboxHealth mailbox={mailbox} />" in _read(PAGES / "MailboxesPage.tsx")


# ---- insights (phase 13) -------------------------------------------------------------------------

COMPONENTS = SRC / "components" / "engagement"


def test_the_screens_never_handle_anything_that_could_name_another_workspace():
    """The server applies D26; the client must not even have the fields that would undo it."""
    for path in list(COMPONENTS.glob("*.ts*")) + list(PAGES.glob("*.tsx")) + [SRC / "lib" / "types.ts"]:
        source = _read(path)
        assert "workspace_count" not in source and "workspace_keys" not in source, path.name


def test_insights_are_fetched_once_per_list_not_once_per_row():
    for path in (PAGES / "ReviewQueue.tsx", COMPONENTS / "ContactPicker.tsx",
                 COMPONENTS / "AccountConversations.tsx"):
        assert "useContactInsights(" in _read(path), path.name
    api = _read(SRC / "lib" / "api.ts")
    assert 'query: { ids: contactIds.slice(0, 100).join(",") }' in api


def test_a_set_time_step_offers_the_best_time():
    editor = _read(COMPONENTS / "StepsEditor.tsx")
    assert '{step.timing_mode === "manual" && (\n                  <BestTimeHint' in editor
    detail = _read(PAGES / "CampaignDetailPage.tsx")
    assert "bestTime={best.data ?? undefined}" in detail
    # Moving one person converts THEIR clock into the viewer's local input.
    assert "zonedClockToLocalInput(" in detail and "moving.contact_timezone" in detail


# ---- enhancements (phase 14) ---------------------------------------------------------------------

def test_a_referral_reply_offers_the_intro_and_nothing_else_does():
    desk = _read(PAGES / "ReplyDeskPage.tsx")
    assert '{openItem && category === "referral" && <ReferralPanel id={id} />}' in desk
    panel = _read(PAGES / "ReferralPanel.tsx")
    # Spending is announced where it happens: a blank address is looked up, and that costs.
    assert "uses an enrichment credit" in panel
    assert "?tab=review" in panel, "the done state goes straight to where the intro is approved"


def test_writing_again_is_its_own_tab_and_sends_only_on_send():
    desk = _read(PAGES / "ReplyDeskPage.tsx")
    assert '{ value: "restart", label: "Write again"' in desk
    again = _read(PAGES / "WriteAgain.tsx")
    assert again.count("api.restartSend(") == 1
    assert "onClick={send}" in again
    today = _read(COMPONENTS / "TodayPlan.tsx")
    assert 'restart: { label: "Write again"' in today


def test_a_tab_strip_scrolls_rather_than_widening_the_page():
    css = _read(SRC / "components" / "ui" / "Tabs.module.css")
    assert "overflow-x: auto;" in css and "white-space: nowrap;" in css
    # The divider cannot be a border: a scrolling box would clip the selected tab's underline.
    tabs_rule = css.split(".tabs {", 1)[1].split("}", 1)[0]
    assert "border-bottom" not in tabs_rule and "inset 0 -1px 0 var(--border)" in tabs_rule


# ---- the cutover (phase 15) ----------------------------------------------------------------------

def test_discovery_hands_its_selection_to_the_campaign_builder():
    panel = _read(SRC / "components" / "discovery" / "ResultsPanel.tsx")
    assert 'navigate("/engagement/campaigns/new", {' in panel
    assert "state: selectionForCampaign(candidates, selected)" in panel
    assert "launchFromSelection" not in panel and "Cadence" not in panel
    builder = _read(PAGES / "CampaignBuilder.tsx")
    assert "useLocation().state" in builder
    # Added once the campaign exists; drafting and sending still wait for review. A list arrives as
    # itself, so the server expands it and records the campaign's source list.
    assert "campaign.id, handedContacts, handedAccounts, handedList || undefined," in builder


def test_a_campaign_without_a_mailbox_offers_its_owner_one():
    detail = _read(PAGES / "CampaignDetailPage.tsx")
    assert "<MailboxChooser" in detail and "api.setCampaignMailbox(" in detail
    assert "isOwner={c.owner_user_id === currentUserId}" in detail


def test_finished_old_campaigns_are_listed_and_never_opened():
    page = _read(PAGES / "CampaignsPage.tsx")
    assert "<LegacyCampaigns team={team} />" in page
    legacy = page.split("function LegacyCampaigns", 1)[1]
    assert "onRowClick" not in legacy and "api.legacyCampaigns(" in legacy
