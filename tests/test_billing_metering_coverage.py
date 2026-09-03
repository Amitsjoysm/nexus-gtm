# tests/test_billing_metering_coverage.py
"""A capability that carries a price must be charged somewhere.

The rate card is the SuperAdmin's statement of what an action costs. A line that no call site
ever meters is not a price — it is a number on a screen, and the action it names runs free
forever with nothing reporting it. That has happened here repeatedly: `ai.scoring` ran 4,090
times unbilled, `enrich.*` were priced and metered nowhere, and `workflow.orchestration_run` was
catalogued, priced and carried by a module gate that reached the nav item and the route guard but
not the API.

This test asserts coverage STRUCTURALLY, so the next priced capability cannot be added without
either a call site or an explicit, reasoned exemption.

Two legitimate reasons a priced capability has no call site of its own:

* **Bundled.** Its cost is already inside a parent capability's price. `verify.email` inside
  `enrich.contact` is the worked example — the card prices that at 4 credits with the COGS line
  "search + finder + verify", so metering the verification again would charge twice for one
  action, against this package's own "one price per request" rule.
* **Gauge.** A level rather than an event (`seat.member`, `platform.storage`); it is read, never
  incremented.

Everything else must be metered.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Where the catalog/price definitions live; a mention there is not a call site.
_DEFINITION_FILES = (
    "nexus/billing/catalog.py", "nexus/billing/rates.py", "nexus/billing/plans.py",
    "nexus/billing/entitlements.py", "nexus/api/routers/admin_billing.py",
    "nexus/api/routers/admin_billing_write.py", "nexus/api/routers/admin_features.py",
    "nexus/api/routers/billing.py",
)

BUNDLED = {
    # Priced for reporting and for standalone use, but their cost sits inside a parent action.
    "verify.email": "inside enrich.contact (card COGS: 'search + finder + verify')",
    "enrich.linkedin_finder": "inside enrich.contact",
    "enrich.source_committee": "inside enrich.contact / campaign sourcing",
    "search.web": "inside the capability that issued the search",
    "signal.news_scan": "inside the account refresh that ran the scan",
    "signal.rss_scan": "inside the account refresh that ran the scan",
    "signal.stored": "carried by module.signals; a stored signal is not a request",
    "inbox.task": "carried by module.signals; created by ingestion, not requested",
    "ai.tokens": "recorded alongside the flat per-action charge, never instead of it",
    "ai.premium_model": "a model choice, surcharged on the action that used it",
    "workflow.orchestration_step": "inside workflow.orchestration_run",
    "discovery.icp_daily": "the sweep bills discovery.account_added per account added",
    "notify.in_app": "notifying someone of work already billed is not a second sale",
    "notify.email_digest": "as notify.in_app",
    "notify.slack": "as notify.in_app",
    "notify.webhook": "as notify.in_app",
    "report.analytics": "reading your own data back is not a billable request",
    "report.cadence": "as report.analytics",
    "integration.crm_connection": "connecting is setup, not usage",
}
GAUGES = {"platform.storage"}

# Priced, user-reachable, and STILL NOT CHARGED. Listed rather than silently tolerated so the
# revenue gap is a number someone can look at, and so a NEW gap fails this test instead of
# joining an invisible pile. Each is a real action a customer can take today that costs us money
# and bills them nothing.
#
# Removing an entry requires a call site, not an edit.
KNOWN_GAPS = {
    "outreach.campaign": "a campaign run bills nothing; only its individual sends now do",
    "outreach.cadence_touch": "as outreach.campaign",
    "outreach.email_draft_save": "saving a draft to the customer's IMAP is unmetered",
    "outreach.sep_push": "pushing to a sequencer is unmetered",
    "automation.play_run": "a play fires its actions unbilled",
    "automation.account_refresh": "the refresh sweep is unbilled by design elsewhere; unresolved",
    "integration.crm_sync": "every CRM sync is unmetered",
    "discovery.lookalike_contact": "in-workspace ranking, no external spend, but priced at 2",
    "ai.personalization_fetch": "provider is stub; nothing to bill until Apify is switched on",
    "api.request": "no public API surface ships yet",
    "calling.brief": "telephony provider is stub",
    "calling.minutes": "telephony provider is stub",
    "calling.task": "telephony provider is stub",
    "network.search": "network graph unreachable until a source is connected",
    "network.persons": "as network.search",
    "network.intro_paths": "as network.search",
    "network.source_sync": "as network.search",
    "network.linkedin_import": "as network.search",
}


def _priced() -> dict:
    from nexus.billing.rates import RATE_SEED

    return {r["capability_id"]: r for r in RATE_SEED
            if not r["capability_id"].startswith("module.")}


def _metered_capabilities() -> set[str]:
    """Every capability id reachable from a metering call, including via a constant or a map."""
    found: set[str] = set()
    priced = set(_priced())
    for path in ROOT.glob("nexus/**/*.py"):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in _DEFINITION_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # A capability named anywhere in a module that also meters is treated as covered: the id
        # commonly arrives through a module constant or an {agent: capability} map.
        if "metered(" not in text and "check_and_meter(" not in text \
                and "record_usage(" not in text:
            continue
        try:
            ast.parse(text)
        except SyntaxError:
            continue
        for cid in priced:
            if f'"{cid}"' in text or f"'{cid}'" in text:
                found.add(cid)
    return found


def test_every_priced_capability_is_billed_bundled_or_a_gauge():
    priced = _priced()
    metered = _metered_capabilities()
    unaccounted = sorted(
        cid for cid in priced
        if cid not in metered and cid not in BUNDLED and cid not in GAUGES
        and cid not in KNOWN_GAPS
    )
    assert not unaccounted, (
        "these capabilities carry a price nobody is charged:\n"
        + "\n".join(
            f"  {c:32} {priced[c].get('credits_per_unit')} credits/unit" for c in unaccounted
        )
        + "\n\nEither meter them at a call site, or record them in BUNDLED/GAUGES with the "
          "reason their cost is already covered."
    )


def test_the_exemption_lists_do_not_rot():
    """An exemption for a capability that no longer exists hides the next real gap."""
    priced = set(_priced())
    stale = sorted((set(BUNDLED) | GAUGES | set(KNOWN_GAPS)) - priced)
    assert not stale, f"exemptions for capabilities that are no longer priced: {stale}"


def test_the_unbilled_gap_is_recorded_and_not_growing():
    """A ceiling on how much priced surface may go uncharged.

    Not zero, because closing every one of these is a body of work rather than an edit — but a
    fixed number, so the next unmetered capability has to be argued for rather than added.
    """
    priced = _priced()
    unbilled = sorted(set(KNOWN_GAPS) & set(priced))
    assert len(unbilled) <= 18, (
        f"{len(unbilled)} priced capabilities are unbilled; the recorded ceiling is 18"
    )
