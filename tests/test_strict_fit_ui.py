"""The strict-fit switch on Settings > Workspace (2026-10-15). There is no frontend test runner, so
these read the source, as the other UI tests here do.

It mirrors the reply switches: a choice the plan does not cover says so, the switch is disabled only
while it is off and not included (one already on can always be turned off), and the bar it holds
candidates to is the number the server reports, never a literal.
"""
from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{rel} is missing — was it moved?"
    return path.read_text(encoding="utf-8")


def test_the_switch_sits_beside_the_daily_discovery_target():
    page = _read("pages/SettingsPage.tsx")
    assert "<StrictFitControl" in page
    assert page.index("<IcpDailyCountControl") < page.index("<StrictFitControl")


def test_it_saves_through_the_automation_endpoint():
    assert "setIcpStrictFit(" in _read("pages/SettingsPage.tsx")
    api = _read("lib/api.ts")
    assert "setIcpStrictFit(enabled: boolean" in api and "icp_strict_fit: enabled" in api


def test_a_plan_without_it_says_so_and_one_already_on_can_be_turned_off():
    page = _read("pages/SettingsPage.tsx")
    assert "Not in force: this workspace's plan no longer includes strict fit." in page
    assert '<Link to="/settings/billing">' in page
    assert "disabled={saving || (!available && !on)}" in page


def test_the_bar_is_the_servers_number():
    page = _read("pages/SettingsPage.tsx")
    assert "settings.icp_min_fit" in page
    types = _read("lib/types.ts")
    for field in ("icp_strict_fit?: boolean", "icp_strict_fit_available?: boolean",
                  "icp_min_fit?: number"):
        assert field in types, field
