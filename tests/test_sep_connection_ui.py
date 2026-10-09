# tests/test_sep_connection_ui.py
"""The Integrations page connects a sequence platform (Salesloft, Outreach) to the workspace.

`/integrations/sep/connection` and the Outreach OAuth start had existed on the server with no
screen: the page still showed only the manual "push one contact" form, so a workspace could not
connect its own platform from the product. No frontend runner here, so this reads the source, like
`test_the_dropdown_offers_exactly_the_providers_the_server_accepts` does for the CRM list.
"""
from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path("frontend/src/pages/IntegrationsPage.tsx")


def test_the_platform_dropdown_offers_exactly_what_the_server_accepts():
    from nexus.integrations.sep_credentials import KNOWN_SEP_PROVIDERS

    src = SRC.read_text(encoding="utf-8")
    block = re.search(r"const SEP_PROVIDERS[^=]*=\s*\[(.*?)\];", src, re.S)
    assert block, "SEP_PROVIDERS not found: was it renamed?"
    assert set(re.findall(r'value:\s*"([a-z_]+)"', block.group(1))) == set(KNOWN_SEP_PROVIDERS)


def test_an_oauth_only_platform_is_offered_a_connect_button_not_a_key_field():
    """The server refuses an API key for Outreach; a key box for it is a form that always fails."""
    from nexus.integrations.sep_credentials import OAUTH_ONLY_SEP_PROVIDERS

    src = SRC.read_text(encoding="utf-8")
    for provider in OAUTH_ONLY_SEP_PROVIDERS:
        assert f'<OAuthConnectButton kind="sep" provider="{provider}"' in src
    assert "api.oauthStart(kind, provider)" in src


def test_the_page_shows_the_connection_card_and_keeps_the_manual_push():
    src = SRC.read_text(encoding="utf-8")
    assert "<SepConnectionCard />" in src and "<SepCard />" not in src
    assert "<ManualSepPushForm />" in src
    for call in ("api.sepConnection(", "api.setSepConnection(", "api.testSepConnection("):
        assert call in src
    api = pathlib.Path("frontend/src/lib/api.ts").read_text(encoding="utf-8")
    assert '"/integrations/sep/connection/test"' in api
    assert "/integrations/${kind}/oauth/${provider}/start" in api
