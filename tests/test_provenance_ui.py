"""Where a value came from is on the screen a rep checks it on, read from the source (there is no
frontend test runner; see `test_plan_gated_nav.py`).

SDR feedback, 2026-10-26: "show the source of every account and contact so SDRs can validate it",
and "contacts came back not found" with no reason. Each assertion is a promise a later edit could
quietly break.
"""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_every_firmographic_fact_can_name_its_source():
    page = _read("pages/AccountDetailPage.tsx")
    for label, key in (("Industry", "industry"), ("Employees", "employee_count"),
                       ("Country", "country"), ("Domain", "domain")):
        fact = page[page.index(f'label="{label}"'):]
        fact = fact[: fact.index("/>")]
        assert f"source={{src.{key}}}" in fact, label
    assert '<SourceNote source={src.description} field="Description" />' in page
    assert '<SourceNote source={src.tech_stack} field="Tech stack" />' in page


def test_a_shared_source_is_said_once_and_a_different_one_beside_its_value():
    page = _read("pages/AccountDetailPage.tsx")
    assert "commonSource(" in page and "sameSource(entry, shared)" in page
    assert 'Source: <SourceNote source={shared} field="Firmographics" />' in page


def test_each_contact_says_where_the_person_and_the_email_came_from():
    page = _read("pages/AccountDetailPage.tsx")
    assert "<ContactSources contact={c} />" in page
    note = _read("components/provenance/SourceNote.tsx")
    assert "Found via <SourceNote source={person}" in note
    assert "Email: <SourceNote source={email}" in note
    contacts = _read("pages/ContactsPage.tsx")
    assert 'header: "Found via"' in contacts


def test_a_value_from_before_sources_were_kept_says_so():
    note = _read("components/provenance/SourceNote.tsx")
    assert 'source.source === "not_recorded"' in note
    assert "Source not recorded · came with" in note


def test_evidence_opens_in_a_new_tab_and_is_named_for_screen_readers():
    note = _read("components/provenance/SourceNote.tsx")
    assert 'target="_blank" rel="noreferrer noopener"' in note
    assert "aria-label={`${field} source: ${label}, opens in a new tab`}" in note


def test_find_contacts_keeps_its_reason_on_screen():
    """A toast is gone before the rep has read why nobody was found."""
    page = _read("pages/AccountDetailPage.tsx")
    assert "setLastFind(result)" in page
    block = page[page.index("{lastFind && !findingContacts && ("):]
    block = block[: block.index("{titleRecs &&")]
    assert 'role="status"' in block and "{lastFind.message}" in block
    assert 'label="Dismiss"' in block


def test_no_coloured_side_bar_on_the_outcome_note():
    css = _read("pages/AccountDetailPage.module.css")
    block = css[css.index(".findOutcome,"):]
    block = block[: block.index(".findOutcomeIcon")]
    assert "border-left" not in block
