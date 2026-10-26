# tests/test_mcp_guide.py
"""The public setup guide at /help/ai-apps (2026-10-23): the documentation and support address
every AI app directory listing links to. It must answer without sign-in, as real HTML (a reviewer's
link check reads the page, and the SPA would give it an empty shell), and the OAuth discovery
documents must point at it rather than at the signed-in AI apps page."""
from __future__ import annotations

import pytest

from nexus.mcp import guide


async def test_the_guide_answers_without_signing_in(client):
    r = await client.get(guide.PATH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "<title>Use InfoJoy GTM from Claude, ChatGPT and other AI assistants</title>" in page
    for anchor in ("support", "permissions", "your-data", "claude-code"):
        assert f'id="{anchor}"' in page
    assert 'href="https://infojoy.com/legal.html#privacy"' in page
    assert 'href="https://infojoy.com/legal.html#terms"' in page
    assert "<table>" in page and "<pre><code>" in page


def test_no_markdown_is_left_in_the_page():
    page = guide.ai_apps_page()
    for residue in ("**", "```", "|---", "]("):
        assert residue not in page, residue


def test_text_is_escaped_and_only_safe_links_render():
    html = guide.render("A <script>alert(1)</script> & `<b>` [ok](https://infojoy.com)")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<code>&lt;b&gt;</code>" in html and "&amp;" in html
    assert '<a href="https://infojoy.com">ok</a>' in html
    for bad in ("javascript:alert(1)", "http://infojoy.com", "data:text/html,x"):
        with pytest.raises(ValueError):
            guide.render(f"[x]({bad})")


def test_an_unclosed_code_block_fails_rather_than_swallowing_the_page():
    with pytest.raises(ValueError):
        guide.render("text\n```\ncode with no end")


async def test_discovery_documents_point_at_the_public_guide(client):
    prm = (await client.get("/.well-known/oauth-protected-resource")).json()
    asm = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert prm["resource_documentation"] == f"http://test{guide.PATH}"
    assert asm["service_documentation"] == f"http://test{guide.PATH}"
