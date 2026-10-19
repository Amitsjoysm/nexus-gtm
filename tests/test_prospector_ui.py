"""Prospector's screen, checked from source (the repo has no frontend test runner).

The load-bearing property is the masking guarantee carried into the client: the search row type
declares no raw email, phone, domain or LinkedIn field, so no component can render one by
accident. Also pinned: the route is guarded like the nav item, delivery shows a WorkingIndicator,
the filter rail is an accessible accordion, and the export neutralises formulas client-side too.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _interface(source: str, name: str) -> str:
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.S)
    assert match, f"interface {name} not found"
    return match.group(1)


def test_the_search_row_type_declares_no_raw_pii_field():
    fields = re.findall(r"^\s+(\w+)\??:", _interface(_read("lib/prospectorTypes.ts"),
                                                     "ProspectRow"), re.M)
    raw = {"email", "phone", "direct_phone", "domain", "company_url", "linkedin",
           "linkedin_url", "company_linkedin"}
    assert not raw & set(fields), f"raw PII fields on ProspectRow: {raw & set(fields)}"
    assert {"email_masked", "phone_masked", "domain_masked", "linkedin_masked"} <= set(fields)


def test_the_route_is_guarded_like_the_nav_item():
    app = _read("App.tsx")
    route = re.search(r'path="/prospector".*?</Route>|path="/prospector"[^/]*?/>', app, re.S)
    block = app[app.index('path="/prospector"'):app.index('path="/prospector"') + 400]
    assert 'RequirePage page="/prospector"' in block
    assert 'RequireCapability capability="module.prospector"' in block
    assert route or block
    nav = _read("app/nav.tsx")
    assert re.search(r'to: "/prospector".*?capability: "module.prospector"', nav, re.S)


def test_delivery_shows_a_working_indicator():
    assert "WorkingIndicator" in _read("pages/prospector/ConfirmDeliveryDialog.tsx")


def test_the_rail_sections_are_an_accessible_accordion():
    section = _read("pages/prospector/FilterSection.tsx")
    assert "aria-expanded={open}" in section and "aria-controls={panelId}" in section


def test_masked_cells_tell_a_screen_reader_what_is_withheld():
    assert "hidden until saved" in _read("pages/prospector/ResultsTable.tsx")


def test_the_client_side_export_neutralises_formulas():
    source = _read("pages/prospector/savedCsv.ts")
    assert 'FORMULA_LEADS = ["=", "+", "-", "@", "\\t", "\\r"]' in source
    # The same exemption as the server's `csv_export._PLAIN_NUMBER`: phones are written as they are.
    assert r"PLAIN_NUMBER = /^[+-]?[\d\s().-]*\d[\d\s().-]*$/" in source
    assert "!PLAIN_NUMBER.test(text)" in source


def test_every_data_view_has_its_states():
    page = _read("pages/prospector/ProspectorPage.tsx")
    for state in ("EmptyState", "ErrorState", "Skeleton"):
        assert state in page, state
