"""The one-screen call console (6.3, 2026-10-29): what its brief adds, and the screen's own rules.

The brief now carries the person's local time (their own timezone, else where the company is, never
the rep's) and which of the workspace's value props meet what a person in their role usually cares
about (`calling/value_fit.py`). The screen reads them; there is no frontend test runner, so the
screen's rules are pinned by reading its source, as the other `_ui` suites do.
"""
from __future__ import annotations

from pathlib import Path

from nexus.calling.value_fit import MAX_PROPS, value_fit
from tests.conftest import auth, signup, tenant_session

SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"

PROPS = [
    {"name": "Capacity forecasting", "description": "Weekly lane forecasts.",
     "pains_solved": ["Missed capacity forecasts", "Expedite costs at quarter end"]},
    {"name": "Clean shipment data", "description": "Dedupes carrier records.",
     "pains_solved": ["Duplicate carrier records", "Bad data quality in the TMS"]},
    {"name": "Tooling fit", "description": "Connectors for the TMS you run.",
     "pains_solved": ["Process efficiency across tools"]},
]
OPS = "process efficiency, tooling fit, and data quality"   # the role angle for an ops title


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ---- value fit -------------------------------------------------------------------------------------

def test_value_props_are_ranked_by_the_pains_that_meet_the_person():
    fit = value_fit(PROPS, person_context=["VP Operations", OPS, "Logistics"])
    assert [p["name"] for p in fit] == ["Clean shipment data", "Tooling fit", "Capacity forecasting"]
    assert fit[0]["matched"] == ["Bad data quality in the TMS"]
    assert fit[1]["matched"] == ["Process efficiency across tools"]
    assert fit[2]["matched"] == []


def test_every_pain_shown_is_the_workspaces_own_and_none_is_invented():
    fit = value_fit(PROPS, person_context=["VP Operations", OPS])
    offered = {p for vp in PROPS for p in vp["pains_solved"]}
    for vp in fit:
        assert set(vp["pains"]) <= offered and set(vp["matched"]) <= set(vp["pains"])


def test_with_nothing_known_the_workspaces_own_order_stands():
    fit = value_fit(PROPS, person_context=[None, "", None])
    assert [p["name"] for p in fit] == [p["name"] for p in PROPS]
    assert all(not p["matched"] for p in fit)


def test_nameless_or_malformed_props_are_skipped_and_the_list_is_capped():
    many = [{"name": f"Prop {i}", "pains_solved": []} for i in range(6)]
    junk = [{"name": "  "}, "not a dict", {"description": "no name"}, None]
    fit = value_fit(junk + many, person_context=["Director"])
    assert len(fit) == MAX_PROPS and [p["name"] for p in fit] == ["Prop 0", "Prop 1", "Prop 2"]
    assert value_fit([], person_context=["VP"]) == [] and value_fit(None, person_context=[]) == []


# ---- the brief -------------------------------------------------------------------------------------

async def _call(client, slug: str, *, contact_tz: str | None = None, country: str | None = None,
                region: str | None = None, title: str = "VP Operations"):
    from nexus.core.security import decode_access_token
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallTask
    from nexus.relevance.engine import get_or_create_profile

    token = await signup(client, slug=slug, email=f"o@{slug}.io", company=slug.title())
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Globex", domain=f"{slug}.io", industry="Logistics",
                      country=country, region=region)
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Dana Reyes", title=title,
                          phone="+13125550142",
                          custom_fields={"timezone": contact_tz} if contact_tz else {})
        ts.add(contact)
        await ts.flush()
        profile = await get_or_create_profile(ts)
        profile.value_props = PROPS
        task = CallTask(tenant_id=tid, account_id=acc.id, contact_id=contact.id)
        ts.add(task)
        await ts.flush()
        return token, task.id


async def test_their_local_time_comes_from_their_own_timezone_first(client):
    token, task_id = await _call(client, "tzown", contact_tz="Europe/Berlin",
                                 country="United States", region="Illinois")
    brief = (await client.get(f"/api/calling/tasks/{task_id}/brief", headers=auth(token))).json()
    assert (brief["local_timezone"], brief["timezone_source"]) == ("Europe/Berlin", "contact")


async def test_else_from_where_the_company_is(client):
    token, task_id = await _call(client, "tzacc", contact_tz="Mars/Olympus",
                                 country="United States", region="Illinois")
    brief = (await client.get(f"/api/calling/tasks/{task_id}/brief", headers=auth(token))).json()
    assert (brief["local_timezone"], brief["timezone_source"]) == ("America/Chicago", "account")


async def test_and_never_a_guess_when_neither_says(client):
    token, task_id = await _call(client, "tznone")
    brief = (await client.get(f"/api/calling/tasks/{task_id}/brief", headers=auth(token))).json()
    assert brief["local_timezone"] is None and brief["timezone_source"] is None


async def test_the_brief_maps_our_value_props_to_their_likely_pains(client):
    token, task_id = await _call(client, "vfit", country="United States")
    from nexus.personalization.brief import role_profile

    brief = (await client.get(f"/api/calling/tasks/{task_id}/brief", headers=auth(token))).json()
    # A VP of Operations runs operations (W7's whole-word roles: it used to read as RevOps), and
    # the pains that are theirs are matched on their function's own words, not only the prose angle.
    assert brief["likely_pain"] == role_profile("VP Operations", None)["angle"]
    assert brief["value_fit"][0]["name"] == "Tooling fit"
    assert brief["value_fit"][0]["matched"] == ["Process efficiency across tools"]
    assert brief["value_fit"][0]["description"] == "Connectors for the TMS you run."


async def test_the_brief_pitches_the_service_that_found_the_account(client):
    """W8 meets 6.3: the value props are the account's service's, not the main service's."""
    from nexus.core.security import decode_access_token
    from nexus.models.account import Account
    from nexus.models.relevance import IcpService

    token, task_id = await _call(client, "vsvc", country="United States")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        service = IcpService(tenant_id=tid, name="Freight audit", icp={}, product_context="",
                             value_props=[{"name": "Freight audit", "description": "Audits bills.",
                                           "pains_solved": ["Bad data quality in the TMS"]}])
        ts.add(service)
        await ts.flush()
        account = await ts.first(Account, Account.name == "Globex")
        account.service_id = service.id
    brief = (await client.get(f"/api/calling/tasks/{task_id}/brief", headers=auth(token))).json()
    assert [v["name"] for v in brief["value_fit"]] == ["Freight audit"]


async def test_a_workspace_with_no_value_props_gets_an_empty_list_not_an_error(client):
    from nexus.core.security import decode_access_token
    from nexus.relevance.engine import get_or_create_profile

    token, task_id = await _call(client, "vnone")
    async with tenant_session(decode_access_token(token)["tid"]) as ts:
        (await get_or_create_profile(ts)).value_props = []
    r = await client.get(f"/api/calling/tasks/{task_id}/brief", headers=auth(token))
    assert r.status_code == 200 and r.json()["value_fit"] == []


# ---- the screen ------------------------------------------------------------------------------------

def test_the_queue_and_a_call_are_never_on_screen_together():
    page = _read("pages/CallsPage.tsx")
    call = page[page.index("if (selected) {"):page.index("<PageHeader")]
    assert 'layout="screen"' in call and "DataTable" not in call
    assert "onExit={() => setSelectedId(null)}" in call


def test_the_modals_keep_the_stacked_console():
    console = _read("components/CallConsole.tsx")
    assert 'layout = "stacked"' in console
    for page in ("pages/AccountDetailPage.tsx", "pages/ContactsPage.tsx"):
        assert 'layout="screen"' not in _read(page), page


def test_the_screen_is_one_fixed_grid_sized_to_the_window():
    console = _read("components/CallConsole.tsx")
    css = _read("components/CallConsole.module.css")
    query = '(min-width: 1200px) and (min-height: 640px)'
    # The measuring hook and the grid switch at the same size, or one would fit a stacked layout.
    assert f'const SCREEN_QUERY = "{query}"' in console and f"@media {query}" in css
    assert "useFitToScroller(rootRef, screen)" in console
    for row in ('"head head head"', '"them script talk"', '"past script reply"', '"log log log"'):
        assert row in css, row


def test_long_text_scrolls_inside_its_panel_never_the_page():
    css = _read("components/CallConsole.module.css")
    body = css[css.index(".screen .panelBody {"):]
    body = body[:body.index("}")]
    assert "overflow-y: auto" in body and "position: relative" in body and "min-height: 0" in body


def test_the_campaigns_script_is_still_read_as_written():
    console = _read("components/CallConsole.tsx")
    # The campaign's or the SDR's own (2026-11-04), whichever is shown: verbatim either way.
    assert "<pre className={styles.ownScript}>{shown.text}</pre>" in console
    assert "const shown = showMine ? myScript : ownScript;" in console
    assert "api.callTaskCampaignScript(" in console


def test_past_calls_come_from_the_existing_calling_endpoint():
    console = _read("components/CallConsole.tsx")
    assert ".contactCallActivities(task.contact_id, ctrl.signal)" in console
    # Session C builds a general activity endpoint; the console must not depend on it.
    assert "/activity" not in console and "contactActivity(" not in console


def test_the_keys_for_a_call_and_where_they_stop():
    console = _read("components/CallConsole.tsx")
    for key in ('key === "c"', 'key === "e"', "/^[1-9]$/.test(key)", 'key === "escape"',
                '(e.ctrlKey || e.metaKey) && e.key === "Enter"'):
        assert key in console, key
    # Typing a note or an open dialog owns the keyboard; the keys exist only on the Calls page.
    assert "closest(\"input, textarea, select, [contenteditable='true']\")" in console
    assert "document.querySelector('[role=\"dialog\"]')" in console
    assert "if (!screen) return;" in console


def test_the_call_clock_is_shown_and_never_billed():
    console = _read("components/CallConsole.tsx")
    log = console[console.index("async function logDisposition"):console.index("const dial = telHref")]
    assert "duration_s" not in log   # platform minutes are Twilio's measured ones


def test_an_sdr_can_upload_their_own_script_from_the_console():
    """Asked by the SDR team (2026-11-04): "upload a script which is visible to the SDR during
    calls". The upload sits in the script panel itself, and a call with a campaign script too
    offers both, the SDR's first."""
    src = _read("components/CallConsole.tsx")
    assert "api.uploadMyCallScript(file)" in src and 'accept=".docx,.txt"' in src
    assert "api.callTaskMyScript(task.id" in src
    assert '"Upload my script"' in src and 'aria-label="Which script to read"' in src
    assert 'useState<"mine" | "campaign">("mine")' in src, "their own script must come first"
