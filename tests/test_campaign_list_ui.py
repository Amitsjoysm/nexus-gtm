"""A saved list can be chosen as a campaign's audience from the campaign screens themselves.

Reported 2026-10-19: "users are not able to add a created list to campaigns; there is no UI to
select a list in a campaign". New campaign had no audience step at all, and the campaign's Add
people card offered lists only as a filter for ticking people one by one, so a whole list could not
be added (nor recorded as the campaign's source) except from the list page's own button. The server
has always taken a ``list_id`` (``POST /engagement/campaigns/{id}/contacts``), expanding it to the
people with an email address and recording it as the source.

There is no frontend test runner, so these read the source, like the other UI tests.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_one_list_chooser_serves_both_screens():
    chooser = _read("components/engagement/ListChooser.tsx")
    assert "api.listSavedLists" in chooser
    # Both kinds, each said in its own words: people on a contact list, companies on an account list.
    assert "contact" in chooser and "companies" in chooser and "people" in chooser
    for screen in ("pages/engagement/CampaignBuilder.tsx", "pages/engagement/CampaignDetailPage.tsx"):
        assert "ListChooser" in _read(screen), screen


def test_new_campaign_offers_a_list_and_adds_it_as_the_source():
    builder = _read("pages/engagement/CampaignBuilder.tsx")
    assert "Who it goes to" in builder
    # The list page's handover pre-selects it; the chosen list (not only the handed one) is sent.
    assert re.search(r"useState\(\s*handover\.listId \?\? \"\"\s*\)", builder)
    assert re.search(r"addCampaignContacts\(\s*campaign\.id,[^)]*listId", builder)


def test_a_whole_list_can_be_added_to_an_existing_campaign():
    detail = _read("pages/engagement/CampaignDetailPage.tsx")
    assert "Add a whole list" in detail
    assert re.search(r"addCampaignContacts\(\s*campaignId,\s*\[\],\s*\[\],\s*\w+", detail)
    # The per-person picker stays, under its own heading.
    assert "ContactPicker" in detail and "Or choose people" in detail
