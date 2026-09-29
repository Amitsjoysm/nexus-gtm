"""The Lists screens, discovery's results panel and the OpenAI-compatible endpoint field, checked by
reading their source (there is no frontend test runner; see `test_plan_gated_nav.py`).

Each assertion is a promise the screen makes that a later edit could quietly break.
"""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_a_list_has_its_own_page_behind_the_same_gate_as_lists():
    app = _read("App.tsx")
    route = app[app.index('path="/lists/:listId"'):]
    route = route[: route.index("/>")]
    assert 'capability="module.lists"' in route and "<ListDetailPage" in route


def test_the_list_page_starts_a_campaign_from_the_list_itself():
    """The list id travels, not a snapshot of ids: the server expands it and records the source."""
    detail = _read("pages/ListDetailPage.tsx")
    assert 'navigate("/engagement/campaigns/new"' in detail and "listId: plist.id" in detail
    builder = _read("pages/engagement/CampaignBuilder.tsx")
    assert "handedList || undefined" in builder
    api = _read("lib/api.ts")
    assert "list_id: listId" in api


def test_only_the_lists_owner_or_a_manager_sees_the_editing_controls():
    """The server decides (`can_edit`); the screen only follows it."""
    detail = _read("pages/ListDetailPage.tsx")
    assert "plist.can_edit &&" in detail
    modal = _read("components/lists/AddToListModal.tsx")
    assert "l.can_edit" in modal


def test_every_selection_surface_offers_add_to_list_with_the_right_kind():
    assert '<AddToListModal' in _read("pages/AccountsPage.tsx")
    assert 'kind="account"' in _read("pages/AccountsPage.tsx")
    assert 'kind="contact"' in _read("pages/ContactsPage.tsx")
    panel = _read("components/discovery/ResultsPanel.tsx")
    assert '<AddToListModal' in panel and 'kind={people ? "contact" : "account"}' in panel


def test_discovery_says_what_it_saved_and_no_longer_starts_a_send_run():
    """Discovery persists what it finds. The panel says so and links there; the Research button,
    which drafted and queued a send through the old path, is gone."""
    panel = _read("components/discovery/ResultsPanel.tsx")
    assert "research_account" not in panel and "Research" not in panel.split("*/", 3)[-1]
    assert "added to your" in panel and '"/accounts?source=found"' in panel
    accounts = _read("pages/AccountsPage.tsx")
    assert '{ value: "found", label: "Found by AI" }' in accounts
    assert 'params.get("source")' in accounts


def test_the_account_page_says_refresh_now_not_run_pipeline():
    page = _read("pages/AccountDetailPage.tsx")
    assert "Refresh now" in page and ">\n                  Run pipeline" not in page


def test_checkboxes_do_not_open_the_row_they_sit_in():
    """Accounts and Contacts rows open a record on click; ticking one must not navigate away."""
    box = _read("components/ui/Checkbox.tsx")
    assert "e.stopPropagation()" in box


def test_the_provider_panel_sets_the_openai_compatible_endpoint():
    tab = _read("pages/admin/ProviderKeysTab.tsx")
    assert "state.base_url_editable &&" in tab
    assert "api.setProviderBaseUrl(provider" in tab
    assert "Endpoint URL" in tab
