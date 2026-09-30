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
    # Copy rows are built from (label, value) pairs in PasteList since the tab became editable.
    for label in ("Authorised redirect URI", "Redirect URI (Web)", "Push endpoint (also the OIDC audience)",
                  "Notification URL", "Scopes", "API permissions"):
        assert f'["{label}"' in tab, f"{label} cannot be copied"
    assert "<CopyRow key={label} label={label} value={value} />" in tab
    assert "navigator.clipboard.writeText" in tab
    assert "app.missing.map" in tab, "what is missing is not listed"
    # Secrets are write-only: typed into password boxes, never read back from the server. The
    # response type carries a hint and nothing else.
    assert "app.client_secret" not in tab
    # The only `...secret}` in the tab is a password box bound to what the operator typed.
    bound = [tab[max(0, i - 60):i] for i in range(len(tab)) if tab.startswith("secret}", i)]
    assert bound and all('type="password" value={f.values.' in b for b in bound), bound
    assert tab.count('type="password"') == 2 and 'autoComplete="new-password"' in tab
    types = _read("lib/types.ts")
    setup = types[types.index("export interface MailboxAppSetup"):]
    setup = setup[:setup.index("}")]
    assert "secret_hint" in setup and "client_secret" not in setup


def test_the_client_knows_the_setup_shape():
    assert 'this.request<EngagementSetup>("/admin/engagement/setup"' in _read("lib/api.ts")
    types = _read("lib/types.ts")
    assert "export interface EngagementSetup" in types
    assert "export interface MailboxAppSetup" in types
