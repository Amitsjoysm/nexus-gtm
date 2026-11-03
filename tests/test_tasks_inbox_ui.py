"""The SDR round 2 screens, checked by reading their source (W3, 5.2, 6.1, 6.4).

There is no frontend test runner, so like `test_engagement_screens_ui.py` these pin the promises the
screens make: a call may lead a sequence, the Inbox is one list in four sections with rows that open
in place, the history panel mounts with one line, and Mail reads but never sends.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
PAGES = SRC / "pages"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_step_editor_lets_a_call_lead_and_names_the_opening_email_by_channel():
    editor = _read(SRC / "components" / "engagement" / "StepsEditor.tsx")
    assert "The first step must be an email" not in editor
    assert 'channel: "email", delay_business_days: 0' not in editor, \
        "moving or removing a step must not turn a leading call into an email"
    assert 'return "Add at least one email step."' in editor, "mirrors validate_steps"
    labels = _read(SRC / "components" / "engagement" / "labels.ts")
    assert "export function stepLabel(" in labels
    for page in ("CampaignDetailPage.tsx", "SequenceTemplatesPage.tsx"):
        source = _read(PAGES / "engagement" / page)
        assert 'i === 0 ? "Opening email"' not in source, page
        assert "stepLabel(" in source, page


def test_the_inbox_is_one_list_in_four_sections_that_fold():
    page = _read(PAGES / "InboxPage.tsx")
    for key, title in (("calls", "Calls"), ("emails", "Emails to send or follow up"),
                       ("replies", "Replies"), ("signals", "Signals")):
        assert f'key: "{key}", title: "{title}"' in page or \
            re.search(rf'key: "{key}",\s*title: "{re.escape(title)}"', page), key
    # Both sources: the engine's due steps and the signal tasks, with the signal window.
    assert "api.engagementToday(s)" in page
    assert "listInbox(view, signal, windowDays)" in page
    # Today first, then overdue, keeping the server's urgency order inside each.
    assert 'r.due === "today" ? 0 : 1' in page
    # A section folds from a real button that says whether it is open; the choice is remembered.
    assert "aria-expanded={!isCollapsed}" in page and "aria-controls={panelId}" in page
    assert "window.localStorage.setItem(COLLAPSED_KEY" in page
    # With the engine off, only Signals show.
    assert 's.key === "signals" || engineOn !== false' in page


def test_an_inbox_row_opens_in_place_to_the_person_the_company_and_the_history():
    page = _read(PAGES / "InboxPage.tsx")
    assert "aria-expanded={open}" in page and "data-task-row" in page
    assert "<td colSpan={4} id={detailId}>" in page
    assert "<ActivityPanel contactId={row.contactId} compact />" in page
    assert "<ActivityPanel accountId={companyId} compact />" in page
    assert "api.getContact(row.contactId, s)" in page


def test_the_history_panel_mounts_with_one_line_on_each_page():
    account = _read(PAGES / "AccountDetailPage.tsx")
    assert account.count("<ActivityPanel") == 1
    assert '<Card padding="lg"><ActivityPanel accountId={id} /></Card>' in account
    contacts = _read(PAGES / "ContactsPage.tsx")
    assert contacts.count("<ActivityPanel") == 1
    assert "{emailFor && <ActivityPanel contactId={emailFor.id} compact />}" in contacts
    panel = _read(SRC / "components" / "activity" / "ActivityPanel.tsx")
    for state in ("ErrorState", "Skeleton", "No calls or emails yet"):
        assert state in panel, state


def test_the_dashboard_summarises_today_and_sends_people_to_the_inbox():
    today = _read(SRC / "components" / "engagement" / "TodayPlan.tsx")
    assert '<ButtonLink to="/inbox"' in today and "items.slice(0, PREVIEW)" in today
    assert "{engineOn && <TodayPlan />}" in _read(PAGES / "DashboardPage.tsx")


def test_mail_reads_every_conversation_and_answers_on_the_reply_desk():
    page = _read(PAGES / "engagement" / "MailPage.tsx")
    assert "api.inboxThreads(filters, s)" in page and "api.inboxThread(threadId, s)" in page
    for control in ('aria-label="Mailbox"', 'aria-label="Campaign"', "Replied", "Waiting on you"):
        assert control in page, control
    # Replying is the reply desk's: Mail links there and has no send of its own.
    assert "<ButtonLink to={replyLink}" in page
    for send in ("deskSend(", "restartSend(", "sendEmailToContact(", "deskSaveDraft("):
        assert send not in page, f"Mail must not grow a send of its own: {send}"
    api = _read(SRC / "lib" / "api.ts")
    block = api[api.index("inboxThreads("):api.index("inboxThread(threadId")]
    assert "method:" not in block


def test_mail_is_a_menu_page_behind_the_engine_and_the_campaigns_module():
    nav = _read(SRC / "app" / "nav.tsx")
    assert re.search(r'to: "/engagement/mail", label: "Mail", icon: <MailIcon />,\s*'
                     r'capability: "module.campaigns", engine: "on"', nav)
    app = _read(SRC / "App.tsx")
    route = app[app.index('path="/engagement/mail"'):]
    route = route[:route.index("/>")]
    assert '<RequirePage page="/engagement/mail">' in route and '<RequireEngine name="Mail">' in route
    assert '<RequireCapability capability="module.campaigns" name="Mail">' in route


def test_an_inbox_call_opens_that_call():
    calls = _read(PAGES / "CallsPage.tsx")
    assert 'useState<string | null>(params.get("task"))' in calls
