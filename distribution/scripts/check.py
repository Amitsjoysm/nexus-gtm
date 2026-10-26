#!/usr/bin/env python
"""Check every listing in this repository against what each directory enforces.

    python scripts/check.py            # offline: structure, limits, consistency, icons
    python scripts/check.py --online   # also open every public link and the live server

Standard library only, so it runs anywhere and in CI. Exit code 0 means nothing is wrong. The
limits come from each directory's own documentation (checked 2026-10-23); a directory's portal
runs more checks than this, so a pass here means "ready to upload", not "approved".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import struct
import sys
import urllib.error
import urllib.request
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "infojoy-gtm"
NAME = "infojoy-gtm"
PRIVACY = "https://infojoy.com/legal.html#privacy"
TERMS = "https://infojoy.com/legal.html#terms"
GUIDE = "https://gtm.infojoy.com/help/ai-apps"
SUPPORT = GUIDE + "#support"
FORBIDDEN_KEYS = {"headers", "clientSecret", "client_secret", "test_credentials",
                  "reviewer_instructions"}
SYSTEM_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"}
RESERVED_NAMES = {"claude", "anthropic", "official", "plugin", "mcp", "test"}

problems: list[str] = []
notes: list[str] = []


def fail(where: str, message: str) -> None:
    problems.append(f"{where}: {message}")


def load(rel: str) -> dict:
    path = ROOT / rel
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(rel, f"cannot read JSON ({exc})")
        return {}


def limit(where: str, field: str, value, most: int, least: int = 1) -> None:
    if not isinstance(value, str) or not least <= len(value) <= most:
        size = len(value) if isinstance(value, str) else "missing"
        fail(where, f"{field} must be {least}-{most} characters (is {size})")


def https(where: str, field: str, value) -> None:
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > 1024:
        fail(where, f"{field} must be an https URL of at most 1024 characters")


def walk(obj, path=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"{path}.{key}", key, value
            yield from walk(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from walk(value, f"{path}[{i}]")


def no_secrets(where: str, data: dict) -> None:
    for path, key, value in walk(data):
        if key in FORBIDDEN_KEYS and value:
            fail(where, f"{path} must not be in a published package")


# ---- PNG, read without Pillow ----------------------------------------------------------------

def png(rel: str) -> tuple[int, int, bytes | None]:
    """(width, height, RGBA pixels or None when not 8-bit RGBA)."""
    data = (ROOT / rel).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if (depth, colour, interlace) != (8, 6, 0):
        return width, height, None
    idat, pos = b"", 8
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw, stride, out, prev = zlib.decompress(idat), width * 4, bytearray(), bytearray(width * 4)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b, c = prev[x], prev[x - 4] if x >= 4 else 0
            if kind == 1:
                line[x] = (line[x] + a) & 255
            elif kind == 2:
                line[x] = (line[x] + b) & 255
            elif kind == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line
        prev = line
    return width, height, bytes(out)


def icon(where: str, rel: str, size: int) -> None:
    try:
        width, height, _ = png(rel)
    except (OSError, ValueError) as exc:
        fail(where, f"{rel}: {exc}")
        return
    if (width, height) != (size, size):
        fail(where, f"{rel} must be {size}x{size} (is {width}x{height})")


def contrast(hex_a: str, hex_b: str) -> float:
    def lum(h: str) -> float:
        rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(hex_a), lum(hex_b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---- one section per directory --------------------------------------------------------------

def check_registry() -> str:
    where = "server.json (MCP Registry)"
    entry = load("server.json")
    if entry.get("$schema") != ("https://static.modelcontextprotocol.io/schemas/2025-12-11/"
                                "server.schema.json"):
        notes.append(f"{where}: $schema is not 2025-12-11; run mcp-publisher validate")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", entry.get("name", "")):
        fail(where, "name must be reverse-DNS with one slash")
    limit(where, "description", entry.get("description"), 100)
    limit(where, "title", entry.get("title"), 100)
    if not re.fullmatch(r"\d+\.\d+\.\d+", entry.get("version", "")):
        fail(where, "version must be semantic (x.y.z)")
    https(where, "websiteUrl", entry.get("websiteUrl"))
    for i, item in enumerate(entry.get("icons", [])):
        https(where, f"icons[{i}].src", item.get("src"))
        if not all(re.fullmatch(r"\d+x\d+|any", s) for s in item.get("sizes", [])):
            fail(where, f"icons[{i}].sizes must be WxH or any")
    remotes = entry.get("remotes", [])
    if len(remotes) != 1 or remotes[0].get("type") != "streamable-http":
        fail(where, "exactly one streamable-http remote is expected")
        return ""
    https(where, "remotes[0].url", remotes[0].get("url"))
    no_secrets(where, entry)
    return remotes[0].get("url", "")


def check_claude(server: str) -> None:
    where = "plugins/infojoy-gtm (Claude plugin)"
    manifest = load("plugins/infojoy-gtm/.claude-plugin/plugin.json")
    name = manifest.get("name", "")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name) or name in RESERVED_NAMES:
        fail(where, "name must be lowercase letters, digits and hyphens, and not a reserved word")
    for field in ("description", "author", "version"):
        if not manifest.get(field):
            fail(where, f"plugin.json should set {field}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")):
        fail(where, "version must be semantic (x.y.z); raise it with every release")
    if not (PLUGIN / "LICENSE").is_file() and not manifest.get("license"):
        fail(where, "a LICENSE file or a license field is required")
    # The portal takes the listing icon from here once, on the first save: it must exist before.
    rel = "plugins/infojoy-gtm/.claude-plugin/icon.png"
    try:
        width, height, _ = png(rel)
        if width != height or not 512 <= width <= 2048 or (ROOT / rel).stat().st_size > 2 << 20:
            fail(where, f"{rel} must be a square PNG of 512-2048 px under 2 MB")
    except (OSError, ValueError) as exc:
        fail(where, f"{rel}: {exc}")
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    words = len(re.sub(r"```.*?```", "", readme, flags=re.S).split())
    if words < 40:
        fail(where, f"README.md needs at least 40 words outside code blocks (has {words})")
    servers = load("plugins/infojoy-gtm/.mcp.json").get("mcpServers", {})
    if list(servers) != [NAME] or servers[NAME].get("type") != "http":
        fail(where, ".mcp.json must declare one http server named infojoy-gtm")
    elif servers[NAME].get("url") != server:
        fail(where, ".mcp.json url differs from server.json")
    no_secrets(where, servers)
    files = [p for p in PLUGIN.rglob("*") if p.is_file()]
    if len(files) > 512:
        fail(where, "more than 512 files")
    for path in files:
        if path.name in SYSTEM_FILES:
            fail(where, f"remove the system file {path.relative_to(ROOT)}")
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp") \
                and path.stat().st_size > 256 * 1024:
            fail(where, f"{path.relative_to(ROOT)} is over 256 KiB")
    for skill in sorted((PLUGIN / "skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8").replace("\r\n", "\n")
        front = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
        fields = dict(line.split(": ", 1) for line in front.group(1).splitlines()) if front else {}
        if fields.get("name") != skill.parent.name or not fields.get("description"):
            fail(where, f"{skill.relative_to(ROOT)} needs name (= folder) and description")
    market = load(".claude-plugin/marketplace.json")
    entries = market.get("plugins", [])
    if [e.get("name") for e in entries] != [NAME] or \
            not (ROOT / entries[0].get("source", "")).joinpath(".claude-plugin", "plugin.json").is_file():
        fail(".claude-plugin/marketplace.json", "must list infojoy-gtm with a source that exists")


def check_openai(server: str) -> None:
    where = "openai/ (ChatGPT plugin)"
    manifest = load("openai/plugin.json")
    if manifest.get("name") != NAME:
        fail(where, "name must be infojoy-gtm")
    limit(where, "description", manifest.get("description"), 4000)
    ext = manifest.get("extensions", {}).get("com.openai", {})
    ui = ext.get("interface", {})
    limit(where, "displayName", ui.get("displayName"), 30)
    limit(where, "shortDescription", ui.get("shortDescription"), 30)
    limit(where, "longDescription", ui.get("longDescription"), 4000)
    limit(where, "developerName", ui.get("developerName"), 80)
    if not ui.get("category"):
        fail(where, "category is required")
    caps = ui.get("capabilities", [])
    if len(caps) > 20 or any(len(c) > 120 for c in caps):
        fail(where, "capabilities: at most 20, each at most 120 characters")
    for field in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        https(where, field, ui.get(field))
    if ui.get("privacyPolicyURL") != PRIVACY or ui.get("termsOfServiceURL") != TERMS:
        fail(where, "privacy and terms must be the published legal page")
    prompts = ui.get("defaultPrompt", [])
    prompts = [prompts] if isinstance(prompts, str) else prompts
    if len(prompts) > 3 or len(set(prompts)) != len(prompts) or any(len(p) > 128 for p in prompts):
        fail(where, "defaultPrompt: at most 3, unique, each at most 128 characters")
    if contrast(ui.get("brandColor", "#FFFFFF"), "#FFFFFF") < 2:
        fail(where, "brandColor needs 2:1 contrast against white")
    if contrast(ui.get("brandColorDark", "#212121"), "#212121") < 2:
        fail(where, "brandColorDark needs 2:1 contrast against #212121")
    for field in ("logo", "logoDark", "composerIcon", "composerIconDark"):
        rel = ui.get(field, "")
        if not rel.startswith("./assets/") or not (ROOT / rel[2:]).is_file():
            fail(where, f"{field} must be ./assets/<file> and exist in assets/")
        else:
            icon(where, rel[2:], 512)
    cases = ext.get("review", {}).get("test_cases", {})
    positive, negative = cases.get("positive", []), cases.get("negative", [])
    if len(positive) != 5 or len(negative) != 3:
        fail(where, "review needs exactly 5 positive and 3 negative test cases")
    for case in positive:
        if not all(case.get(k) for k in ("description", "prompt", "tools_triggered",
                                         "expected_behavior")):
            fail(where, "a positive test case lacks a required field")
    for case in negative:
        if not all(case.get(k) for k in ("description", "prompt")):
            fail(where, "a negative test case lacks description or prompt")
    if not ext.get("review", {}).get("demo_recording_url"):
        notes.append(f"{where}: no demo video yet; pass it with build.py --demo-url or in the "
                     "portal's Review details")
    no_secrets(where, manifest)
    servers = load("openai/mcp.json").get("mcpServers", {})
    if len(servers) != 1 or next(iter(servers.values()), {}).get("type") != "streamable-http":
        fail(where, "mcp.json must declare exactly one streamable-http server")
    elif next(iter(servers.values())).get("url") != server:
        fail(where, "mcp.json url differs from server.json")
    no_secrets(where, servers)


def check_gemini(server: str) -> None:
    where = "gemini-extension.json (Gemini CLI)"
    ext = load("gemini-extension.json")
    if ext.get("name") != NAME or not re.fullmatch(r"[a-z0-9-]+", ext.get("name", "")):
        fail(where, "name must be infojoy-gtm (lowercase, dashes)")
    if not ext.get("version") or not ext.get("description"):
        fail(where, "version and description are required")
    if not (ROOT / ext.get("contextFileName", "GEMINI.md")).is_file():
        fail(where, "the context file it names does not exist")
    servers = ext.get("mcpServers", {})
    if list(servers) != [NAME] or servers[NAME].get("httpUrl") != server:
        fail(where, "mcpServers must hold infojoy-gtm with httpUrl equal to server.json")
    no_secrets(where, servers)


def check_microsoft(server: str) -> None:
    where = "microsoft/ (MCP certification)"
    for rel in ("microsoft/manifest.json", "microsoft/mcptools.json"):
        try:
            (ROOT / rel).read_bytes().decode("ascii")
        except UnicodeDecodeError:
            fail(where, f"{rel} must be ASCII only")
    manifest = load("microsoft/manifest.json")
    if manifest.get("manifestVersion") != "1.30":
        fail(where, "manifestVersion must be 1.30")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        manifest.get("id", "")):
        fail(where, "id must be a GUID")
    dev = manifest.get("developer", {})
    limit(where, "developer.name", dev.get("name"), 32)
    for field in ("websiteUrl", "privacyUrl", "termsOfUseUrl"):
        https(where, f"developer.{field}", dev.get(field))
    if dev.get("privacyUrl") != PRIVACY or dev.get("termsOfUseUrl") != TERMS:
        fail(where, "privacy and terms must be the published legal page")
    limit(where, "name.short", manifest.get("name", {}).get("short"), 30)
    limit(where, "name.full", manifest.get("name", {}).get("full"), 100)
    limit(where, "description.short", manifest.get("description", {}).get("short"), 80)
    limit(where, "description.full", manifest.get("description", {}).get("full"), 4000)
    icon(where, "assets/color.png", 192)
    icon(where, "assets/outline.png", 32)
    _, _, pixels = png("assets/outline.png")
    if pixels is None or any(pixels[i + 3] and pixels[i:i + 3] != b"\xff\xff\xff"
                             for i in range(0, len(pixels), 4)):
        fail(where, "outline.png must be white on transparent (8-bit RGBA)")
    connectors = manifest.get("agentConnectors", [])
    remote = connectors[0].get("toolSource", {}).get("remoteMcpServer", {}) if connectors else {}
    if len(connectors) != 1 or remote.get("mcpServerUrl") != server:
        fail(where, "one agent connector whose mcpServerUrl equals server.json")
    auth = remote.get("authorization", {})
    if auth.get("type") != "AzureKeyVault" or not re.fullmatch(
            r"https://[a-z0-9-]{3,24}\.vault\.azure\.net/", auth.get("referenceId", "")):
        fail(where, "authorization must be AzureKeyVault with the vault URI as referenceId")
    if remote.get("mcpToolDescription", {}).get("file") != "mcptools.json":
        fail(where, "mcpToolDescription.file must be mcptools.json")
    tools = load("microsoft/mcptools.json").get("tools", [])
    names = [t.get("name") for t in tools]
    if not tools or len(set(names)) != len(names):
        fail(where, "mcptools.json must list each tool once")
    for tool in tools:
        ann = tool.get("annotations", {})
        if not ann.get("title") or "readOnlyHint" not in ann:
            fail(where, f"tool {tool.get('name')} lacks title or readOnlyHint")
    intro = (ROOT / "microsoft" / "intro.md").read_text(encoding="utf-8")
    if "## Known issues and limitations" not in intro:
        fail(where, "intro.md needs a 'Known issues and limitations' section")


def check_repository() -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts or "dist" in path.relative_to(ROOT).parts[:1]:
            continue
        if path.name in SYSTEM_FILES:
            fail("repository", f"remove the system file {path.relative_to(ROOT)}")
        if re.search(r'[:*?"<>|]|[. ]$', path.name) or \
                path.stem.lower() in {"con", "prn", "aux", "nul"}:
            fail("repository", f"{path.relative_to(ROOT)} is not a valid Windows file name")
    for rel in ("assets/icon.png", "assets/icon-dark.png"):
        icon("assets", rel, 512)


# ---- --online -------------------------------------------------------------------------------

def fetch(url: str, *, method: str = "GET") -> tuple[int, dict, str]:
    request = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None,
                                     headers={"User-Agent": "infojoy-gtm-listing-check",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, dict(response.headers), response.read(400_000).decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read(20_000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {}, str(exc)


def check_online(server: str) -> None:
    where = "online"
    # An anchor each page must carry. The app answers any unknown path with its own shell page
    # and a 200, so a status code alone cannot tell the guide from a page that is not there.
    expected = {PRIVACY: "privacy", TERMS: "terms", SUPPORT: "support",
                GUIDE: "before-you-start"}
    urls = sorted({PRIVACY, TERMS, GUIDE, SUPPORT, "https://infojoy.com/gtm.html",
                   load("server.json").get("icons", [{}])[0].get("src", "")} - {""})
    for url in urls:
        page = url.partition("#")[0]
        status, _, body = fetch(page)
        if status != 200:
            fail(where, f"{page} answered {status or body}")
        elif url in expected and f'id="{expected[url]}"' not in body:
            fail(where, f"{url}: the page has no #{expected[url]} anchor (not deployed?)")
        else:
            notes.append(f"{where}: {url} ok")
    base = server.rsplit("/mcp", 1)[0]
    status, _, body = fetch(f"{base}/.well-known/oauth-protected-resource")
    try:
        resource = json.loads(body).get("resource") if status == 200 else None
    except ValueError:
        resource = None
    if resource != server:
        fail(where, f"protected-resource metadata names {resource!r}, expected {server!r} "
                    "(set the App public address)")
    status, headers, _ = fetch(server, method="POST")
    challenge = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
    if status != 401 or "resource_metadata=" not in challenge:
        fail(where, f"unauthenticated POST {server} should be 401 with WWW-Authenticate "
                    f"resource_metadata (got {status})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--online", action="store_true", help="also open every public link")
    args = parser.parse_args()
    server = check_registry()
    check_claude(server)
    check_openai(server)
    check_gemini(server)
    check_microsoft(server)
    check_repository()
    if args.online:
        check_online(server)
    for note in notes:
        print("note:", note)
    for problem in problems:
        print("FAIL:", problem)
    print("PASS" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
