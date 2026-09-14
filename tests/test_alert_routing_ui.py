"""The alert routing and account owner screens use what the server now supports.

Every piece of this shipped server-side first and was unreachable from the product: a route scoped to
"only my accounts" with no control to set it, workspace rules with no editor, an account owner with
no way to see, claim or reassign it. The failure mode this codebase keeps finding is exactly that —
a capability that exists and that nobody can use.

There is no frontend test runner, so these read the source, as the other UI tests here do.
"""
from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{rel} is missing — was it moved?"
    return path.read_text(encoding="utf-8")


def test_a_route_can_be_limited_to_my_accounts():
    panel = _read("pages/settings/AlertDelivery.tsx")
    assert "Only accounts I own" in panel, "no way to scope a route to my accounts"
    # Changing only a route's timing must carry its scope, or every edit would widen it back to all.
    assert re.search(r"scope:\s*p\.scope", panel), "editing a route's timing drops its scope"


def test_managers_choose_what_a_shared_channel_receives():
    page = _read("pages/AlertSettingsPage.tsx")
    assert "<ChannelRules" in page and "useCanConnectChannels" in page
    rules = _read("components/alerts/ChannelRules.tsx")
    assert "setAlertChannelRules" in rules
    assert "data.channels.map" in rules
    # The category list is the server's, derived from the alert rules, never a copy kept here.
    assert "notificationPreferences" in rules


def test_the_alerts_page_links_to_its_settings():
    assert "/alerts/settings" in _read("pages/AlertsPage.tsx")


def test_an_account_shows_its_owner_and_who_can_change_it():
    page = _read("pages/AccountDetailPage.tsx")
    assert "<AccountOwner" in page, "the account page does not show who owns it"
    owner = _read("components/AccountOwner.tsx")
    assert "setAccountOwner" in owner
    assert "memberDirectory" in owner, "a manager has no way to choose who owns the account"
    for verb in ("Claim", "Release"):
        assert verb in owner, f"a rep cannot {verb.lower()} an account"


def test_the_accounts_list_shows_owners_and_can_narrow_to_mine():
    src = _read("pages/AccountsPage.tsx")
    assert "owner_name" in src, "no owner column"
    assert "My accounts" in src, "no way to narrow the list to my own accounts"
