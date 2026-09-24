"""The Relevance page asks "how many companies now?" after an ICP save. There is no frontend test
runner, so these read the source, like the other UI contract tests here.

Pinned: the question appears only for a save the server says changed the ICP; the count control is a
real radio group; a refused start explains itself (402 points at billing, 409 shows the run that is
already going); progress is polled and stops when the run ends; the server's check, not the quote,
decides whether a request may start; and no internal vendor name reaches the customer.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "frontend" / "src"
PAGE = (ROOT / "pages" / "RelevancePage.tsx").read_text(encoding="utf-8")
PANEL = (ROOT / "pages" / "relevance" / "PopulatePanel.tsx").read_text(encoding="utf-8")
API = (ROOT / "lib" / "api.ts").read_text(encoding="utf-8")


def test_the_question_follows_a_save_that_changed_the_icp():
    assert "<PopulatePanel" in PAGE
    assert "if (updated.icp_changed) setAskingPopulate(true)" in PAGE
    assert "canEdit && (" in PAGE.split("<PopulatePanel")[0][-200:], "admins only, like the ICP"


def test_the_client_calls_the_populate_endpoints():
    for path in ('"/discovery/populate/quote"', '"/discovery/populate"',
                 "`/discovery/populate/${encodeURIComponent(id)}`", '"/discovery/populate/latest"'):
        assert path in API, path


def test_the_count_is_a_keyboard_radio_group_with_the_presets_and_custom():
    assert 'role="radiogroup"' in PANEL and 'role="radio"' in PANEL
    assert "aria-checked={checked}" in PANEL
    assert "const PRESETS = [10, 20, 50, 100] as const;" in PANEL
    assert "const MAX = 500;" in PANEL
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert f'"{key}"' in PANEL, key
    assert "autoFocus" not in PANEL, "arrowing onto Custom must not pull focus out of the group"


def test_a_refused_start_says_what_to_do():
    assert "err.status === 402" in PANEL and 'to="/settings/billing"' in PANEL
    assert "err.status === 409" in PANEL and "getLatestPopulate()" in PANEL


def test_progress_is_polled_and_stops_when_the_run_ends():
    assert "const POLL_MS = 3000;" in PANEL
    assert re.search(r'const active = run\?\.status === "queued" \|\| run\?\.status === "running"',
                     PANEL)
    assert "if (!runId || !active) return;" in PANEL
    assert "window.clearInterval(id)" in PANEL


def test_the_quote_informs_and_the_server_decides():
    assert "disabled={!countValid}" in PANEL
    assert "!quote.enough}" not in PANEL, "a short quote must not block what the server would allow"


def test_the_outcome_says_what_was_charged():
    assert "Nothing was charged." in PANEL
    assert "Charged for the" in PANEL


def test_no_internal_vendor_name_reaches_the_customer():
    for vendor in ("Apify", "apify", "Exa", "harvestapi", "Firecrawl", "Serper"):
        assert not re.search(rf"\b{vendor}\b", PANEL), vendor
