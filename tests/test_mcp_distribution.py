# tests/test_mcp_distribution.py
"""The marketplace artifacts (spec 2026-10-13, task 10): the Claude plugin and the MCP Registry
entry must point at the server this code serves, use the customer-facing name, and pass the
directory's mechanical checks we can run without its portal."""
from __future__ import annotations

import json
import pathlib
import re

from nexus.mcp.brand import PRODUCT_NAME, SERVER_ID

ROOT = pathlib.Path("distribution")
PLUGIN = ROOT / "claude-plugin" / "infojoy-gtm"
VENDORS = ("exa", "apify", "groq", "firecrawl", "serper", "reacher", "harvestapi", "duckduckgo",
           "nexus")


def _json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_manifest_has_what_every_app_and_the_directory_read():
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == SERVER_ID
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", manifest["name"])
    assert manifest["displayName"] == PRODUCT_NAME
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["description"] and manifest["author"]["name"] and manifest["license"]
    assert (PLUGIN / "LICENSE").is_file()


def test_the_plugin_declares_one_remote_https_server_ending_in_mcp():
    servers = _json(PLUGIN / ".mcp.json")["mcpServers"]
    assert list(servers) == [SERVER_ID]
    server = servers[SERVER_ID]
    assert server["type"] == "http"
    assert server["url"].startswith("https://") and server["url"].endswith("/mcp")
    assert "headers" not in server, "no secret may ship inside a plugin"


def test_the_readme_is_long_enough_and_covers_data_and_privacy():
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", readme, flags=re.S)
    assert len(prose.split()) >= 40
    for heading in ("## Data", "## Privacy", "## Setup"):
        assert heading in readme
    assert "never sends an email" in readme.lower()


def test_every_skill_has_front_matter_and_names_only_real_tools():
    from nexus.mcp.tools import TOOLS

    names = {t.name for t in TOOLS}
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    assert len(skills) == 3
    for skill in skills:
        text = skill.read_text(encoding="utf-8")
        front = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
        assert front, skill
        fields = dict(line.split(": ", 1) for line in front.group(1).splitlines())
        assert fields["name"] == skill.parent.name
        assert fields["description"] and not fields["description"].startswith("[")
        mentioned = set(re.findall(r"`([a-z_]+)`", text)) - {"account", "contact", "guidance",
                                                             "request_id", "contact_id",
                                                             "min_strength", "max_age_days",
                                                             "contact_ids"}
        assert mentioned <= names, (skill.name, mentioned - names)


def test_the_registry_entry_points_at_the_same_server():
    entry = _json(ROOT / "mcp-registry" / "server.json")
    plugin_url = _json(PLUGIN / ".mcp.json")["mcpServers"][SERVER_ID]["url"]
    assert entry["remotes"] == [{"type": "streamable-http", "url": plugin_url}]
    assert entry["title"] == PRODUCT_NAME
    assert re.fullmatch(r"[a-z0-9.-]+/[a-z0-9-]+", entry["name"])


def test_the_marketplace_lists_the_plugin():
    market = _json(ROOT / "claude-plugin" / ".claude-plugin" / "marketplace.json")
    assert market["plugins"] == [{
        "name": SERVER_ID, "source": "./infojoy-gtm",
        "description": market["plugins"][0]["description"]}]


def test_nothing_customers_read_names_an_internal_vendor_or_codename():
    files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in (".md", ".json")]
    files.append(pathlib.Path("docs/mcp/setup-guide.md"))
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for word in VENDORS:
            assert not re.search(rf"\b{word}\b", text), (str(path), word)
