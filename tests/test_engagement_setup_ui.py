"""The Control plane shows what to paste into Google Cloud and Azure (spec §12).

There is no frontend test runner, so these read the source, as the other UI tests here do.
"""
from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{rel} is missing"
    return path.read_text(encoding="utf-8")


def test_the_mailbox_apps_tab_is_offered_to_whoever_manages_provider_keys():
    page = _read("pages/AdminBillingPage.tsx")
    assert 'can(PROVIDERS_MANAGE) ? [{ value: "engagement", label: "Mailbox apps" }]' in page
    assert '{tab === "engagement" && can(PROVIDERS_MANAGE) && <EngagementSetupTab />}' in page


def test_every_value_an_operator_pastes_can_be_copied_and_no_secret_is_rendered():
    tab = _read("pages/admin/EngagementSetupTab.tsx")
    assert "api.engagementSetup" in tab
    for label in ("Redirect URI", "Push endpoint", "OIDC audience", "Notification URL", "Scopes"):
        assert f'<CopyRow label="{label}"' in tab, f"{label} cannot be copied"
    assert "navigator.clipboard.writeText" in tab
    assert "app.missing.map" in tab, "what is missing is not listed"
    assert "client_secret" not in tab and "secret}" not in tab


def test_the_client_knows_the_setup_shape():
    assert 'this.request<EngagementSetup>("/admin/engagement/setup"' in _read("lib/api.ts")
    types = _read("lib/types.ts")
    assert "export interface EngagementSetup" in types
    assert "export interface MailboxAppSetup" in types
