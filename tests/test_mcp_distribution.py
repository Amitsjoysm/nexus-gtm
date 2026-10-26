# tests/test_mcp_distribution.py
"""The directory listings (spec 2026-10-13, task 10; listing repo 2026-10-23): the Claude plugin,
the ChatGPT plugin, the Gemini CLI extension, the MCP Registry entry and the Microsoft package must
point at the server this code serves, use the customer-facing name, name only tools that exist,
and pass the directories' mechanical checks we can run without their portals.

``distribution/`` is exported as the public listing repository
(``scripts/export_listing_repo.py``); its own ``scripts/check.py`` runs here too, so a change that
would fail that repository's CI fails this suite first."""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

from nexus.mcp import guide
from nexus.mcp.brand import PRODUCT_NAME, SERVER_ID

ROOT = pathlib.Path("distribution")
PLUGIN = ROOT / "plugins" / "infojoy-gtm"
PRIVACY = "https://infojoy.com/legal.html#privacy"
TERMS = "https://infojoy.com/legal.html#terms"
VENDORS = ("exa", "apify", "groq", "firecrawl", "serper", "reacher", "harvestapi", "duckduckgo",
           "nexus")
# Words in backticks in the skills and GEMINI.md that are arguments, not tool names.
ARGUMENTS = {"account", "contact", "guidance", "request_id", "contact_id", "min_strength",
             "max_age_days", "contact_ids"}


def _json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _server_url() -> str:
    return _json(ROOT / "server.json")["remotes"][0]["url"]


def _tool_names() -> set[str]:
    from nexus.mcp.tools import TOOLS

    return {t.name for t in TOOLS}


def test_the_manifest_has_what_every_app_and_the_directory_read():
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == SERVER_ID
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", manifest["name"])
    assert manifest["displayName"] == PRODUCT_NAME
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["description"] and manifest["author"]["name"] and manifest["license"]
    assert manifest["privacyPolicyUrl"] == PRIVACY
    assert (PLUGIN / "LICENSE").is_file()
    # The portal takes the listing icon from here once, the first time the plugin is saved.
    assert (PLUGIN / ".claude-plugin" / "icon.png").is_file()


def test_the_plugin_declares_one_remote_https_server_ending_in_mcp():
    servers = _json(PLUGIN / ".mcp.json")["mcpServers"]
    assert list(servers) == [SERVER_ID]
    server = servers[SERVER_ID]
    assert server["type"] == "http"
    assert server["url"].startswith("https://") and server["url"].endswith("/mcp")
    assert "headers" not in server, "no secret may ship inside a plugin"


def test_every_listing_points_at_the_same_server():
    url = _server_url()
    assert url == _json(PLUGIN / ".mcp.json")["mcpServers"][SERVER_ID]["url"]
    assert _json(ROOT / "openai" / "mcp.json")["mcpServers"][SERVER_ID] == {
        "type": "streamable-http", "url": url}
    assert _json(ROOT / "gemini-extension.json")["mcpServers"] == {SERVER_ID: {"httpUrl": url}}
    connector = _json(ROOT / "microsoft" / "manifest.json")["agentConnectors"][0]
    assert connector["toolSource"]["remoteMcpServer"]["mcpServerUrl"] == url
    assert url in guide.SOURCE.read_text(encoding="utf-8")


def test_the_readme_is_long_enough_and_covers_data_and_privacy():
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", readme, flags=re.S)
    assert len(prose.split()) >= 40
    for heading in ("## Data", "## Privacy", "## Setup"):
        assert heading in readme
    assert "never sends an email" in readme.lower()
    assert PRIVACY in readme and TERMS in readme


def test_every_skill_has_front_matter_and_names_only_real_tools():
    names = _tool_names()
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    assert len(skills) == 3
    for skill in skills:
        text = skill.read_text(encoding="utf-8").replace("\r\n", "\n")
        front = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
        assert front, skill
        fields = dict(line.split(": ", 1) for line in front.group(1).splitlines())
        assert fields["name"] == skill.parent.name
        assert fields["description"] and not fields["description"].startswith("[")
        mentioned = set(re.findall(r"`([a-z_]+)`", text)) - ARGUMENTS
        assert mentioned <= names, (skill.name, mentioned - names)


def test_the_gemini_context_names_only_real_tools():
    text = (ROOT / "GEMINI.md").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"`([a-z_]+)`", text)) - ARGUMENTS
    assert mentioned and mentioned <= _tool_names(), mentioned - _tool_names()


def test_the_chatgpt_review_cases_call_only_real_tools():
    review = _json(ROOT / "openai" / "plugin.json")["extensions"]["com.openai"]["review"]
    positive, negative = review["test_cases"]["positive"], review["test_cases"]["negative"]
    assert len(positive) == 5 and len(negative) == 3
    for case in positive:
        triggered = {t.strip() for t in case["tools_triggered"].split(",")}
        assert triggered <= _tool_names(), triggered - _tool_names()


def test_the_registry_entry_fits_the_registry_schema():
    """`description` was 137 characters against the schema's 100, so `mcp-publisher publish` would
    have refused the entry (found 2026-10-23)."""
    entry = _json(ROOT / "server.json")
    assert entry["$schema"].endswith("/2025-12-11/server.schema.json")
    assert re.fullmatch(r"[a-z0-9.-]+/[a-z0-9-]+", entry["name"])
    assert entry["title"] == PRODUCT_NAME
    assert 1 <= len(entry["description"]) <= 100
    assert entry["remotes"] == [{"type": "streamable-http", "url": _server_url()}]
    assert entry["websiteUrl"].endswith(guide.PATH)
    assert all(icon["src"].startswith("https://") for icon in entry["icons"])


def test_the_marketplace_lists_the_plugin():
    market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    assert market["plugins"] == [{
        "name": SERVER_ID, "source": "./plugins/infojoy-gtm",
        "description": market["plugins"][0]["description"]}]


def test_the_microsoft_tool_file_is_the_catalogue():
    """Certification pins the tool list; regenerate with scripts/export_mcp_tools.py."""
    from nexus.mcp.tools import TOOLS, describe

    path = ROOT / "microsoft" / "mcptools.json"
    path.read_bytes().decode("ascii")
    assert _json(path) == {"tools": [describe(t, {}) for t in TOOLS]}


def test_every_listing_links_the_legal_pages_and_the_public_guide():
    ui = _json(ROOT / "openai" / "plugin.json")["extensions"]["com.openai"]["interface"]
    assert (ui["privacyPolicyURL"], ui["termsOfServiceURL"]) == (PRIVACY, TERMS)
    assert ui["supportURL"].endswith(f"{guide.PATH}#support")
    developer = _json(ROOT / "microsoft" / "manifest.json")["developer"]
    assert (developer["privacyUrl"], developer["termsOfUseUrl"]) == (PRIVACY, TERMS)
    assert f'id="{guide.slug("Support")}"' in guide.ai_apps_page()


def test_the_listing_repositorys_own_check_passes():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py")],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_nothing_customers_read_names_an_internal_vendor_or_codename():
    files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in (".md", ".json")]
    files.append(guide.SOURCE)
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for word in VENDORS:
            assert not re.search(rf"\b{word}\b", text), (str(path), word)
