# tests/test_mcp_ui.py
"""The AI apps page and the OAuth consent screen (spec 2026-10-13, task 9). There is no frontend
test runner, so these read the source, as the other UI tests do."""
from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path("frontend/src")


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def _words(rel: str) -> str:
    """The source with runs of whitespace collapsed, so a sentence wrapped across JSX lines
    still matches."""
    return " ".join(_read(rel).split())


def test_the_nav_offers_ai_apps_to_every_member_behind_module_mcp():
    nav = _read("app/nav.tsx")
    line = next(l for l in nav.splitlines() if '"/connected-apps"' in l)
    assert 'label: "AI apps"' in line and 'capability: "module.mcp"' in line
    assert "minRole" not in line


def test_the_page_is_hideable_and_its_route_is_guarded():
    from nexus.features.pages import BY_KEY

    assert BY_KEY["ai_apps"].path == "/connected-apps"
    app = _read("App.tsx")
    block = app[app.index('path="/connected-apps"'):][:400]
    assert '<RequirePage page="/connected-apps">' in block
    assert 'capability="module.mcp"' in block


def test_the_consent_route_is_outside_the_app_shell_and_survives_login():
    app = _read("App.tsx")
    consent = app.index('path="/oauth/consent"')
    shell = app.index("<AppShell />")
    assert consent < shell, "the consent screen must not render inside the sidebar shell"
    assert "RequireAuthForConsent" in app[consent - 50:consent + 300]
    # Only the consent page is remembered across login, never an arbitrary address.
    assert 'path.startsWith("/oauth/consent?")' in app


def test_the_secret_is_shown_only_from_the_create_response():
    page = _read("pages/ConnectedAppsPage.tsx")
    assert "created.secret" in page
    reads = re.findall(r"(\w+)\.secret\b", page)
    assert reads and set(reads) == {"created"}, f"a secret read from somewhere else: {reads}"
    assert "It is shown only once" in page


def test_the_consent_screen_names_the_destination_and_flags_unverified_apps():
    page = _words("pages/OAuthConsentPage.tsx")
    assert "view.redirect_host" in page and "you go back to" in page
    assert "Unverified app" in page and "which any" in page
    assert "view.only_loopback" in page
    assert "AI model behind it" in page  # where the data goes
    assert "scopes.length === 0" in page  # cannot allow with nothing ticked


def test_both_screens_share_one_description_of_each_permission():
    for rel in ("pages/ConnectedAppsPage.tsx", "pages/OAuthConsentPage.tsx"):
        assert 'from "@/components/mcp/scopes"' in _read(rel), rel
    scopes = _read("components/mcp/scopes.ts")
    assert "Never sends an email" in scopes and "Spends the workspace's credits" in scopes


def test_the_client_names_the_new_endpoints():
    api = _read("lib/api.ts")
    for name in ("listConnections(", "mcpSetup(", "createMcpKey(", "updateConnection(",
                 "revokeConnection(", "oauthRequest(", "oauthApprove(", "oauthDeny("):
        assert name in api, name


def test_the_screens_use_the_customer_facing_product_name():
    from nexus.mcp.brand import PRODUCT_NAME, SERVER_ID

    brand = _read("lib/brand.ts")
    assert f'PRODUCT_NAME = "{PRODUCT_NAME}"' in brand and f'SERVER_ID = "{SERVER_ID}"' in brand
    for rel in ("pages/ConnectedAppsPage.tsx", "pages/OAuthConsentPage.tsx",
                "components/mcp/scopes.ts"):
        text = _read(rel)
        visible = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        assert "NEXUS" not in visible, rel


def test_the_page_says_when_claude_and_chatgpt_cannot_reach_the_address():
    page = _words("pages/ConnectedAppsPage.tsx")
    assert "setup.reachable_from_internet" in page
    assert "cannot reach it" in page


def test_the_page_says_which_apps_take_an_api_key():
    """Asked "do I need a custom connector to use an API key?": Claude's and ChatGPT's connectors
    sign in with OAuth and take no key; the key goes into the apps that run on your computer."""
    page = _words("pages/ConnectedAppsPage.tsx")
    assert "do not take an API key" in page
    assert page.count("headers: { Authorization: `Bearer ${keyPlaceholder}` }") >= 2  # editors, API
    assert "McpSetup" in _read("lib/types.ts") and "reachable_from_internet: boolean" in _read("lib/types.ts")
