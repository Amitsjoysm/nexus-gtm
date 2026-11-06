#!/usr/bin/env python
"""Build the two upload packages: the ChatGPT plugin ZIP and the Microsoft certification ZIP.

    python scripts/build.py [--demo-url URL] [--key-vault https://<vault>.vault.azure.net/]

Runs scripts/check.py first and stops if it fails. Writes ``dist/``:

- ``infojoy-gtm-openai-<version>.zip``: plugin.json and mcp.json at the root, the Claude plugin's
  skills, and the icons. ``--demo-url`` sets ``review.demo_recording_url`` (required for review,
  or enter it in the portal's Review details).
- ``infojoy-gtm-microsoft-<version>.zip``: manifest.json, color.png, outline.png, mcptools.json and
  intro.md at the root. ``--key-vault`` replaces the Key Vault URI in the manifest.

Standard library only. Entries carry a fixed timestamp, so the same sources build the same bytes.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAMP = (2026, 1, 1, 0, 0, 0)


def _add(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=STAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def _json(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def build_openai(demo_url: str | None) -> pathlib.Path:
    manifest = json.loads((ROOT / "openai" / "plugin.json").read_text(encoding="utf-8"))
    if demo_url:
        if not demo_url.startswith("https://"):
            raise SystemExit("--demo-url must be an https URL")
        manifest["extensions"]["com.openai"]["review"]["demo_recording_url"] = demo_url
    out = DIST / f"infojoy-gtm-openai-{manifest['version']}.zip"
    skills = ROOT / "plugins" / "infojoy-gtm" / "skills"
    with zipfile.ZipFile(out, "w") as archive:
        _add(archive, "plugin.json", _json(manifest))
        _add(archive, "mcp.json", (ROOT / "openai" / "mcp.json").read_bytes())
        for path in sorted(skills.rglob("*")):
            if path.is_file():
                _add(archive, f"skills/{path.relative_to(skills).as_posix()}", path.read_bytes())
        for name in ("icon.png", "icon-dark.png"):
            _add(archive, f"assets/{name}", (ROOT / "assets" / name).read_bytes())
    return out


def build_microsoft(key_vault: str | None) -> pathlib.Path:
    text = (ROOT / "microsoft" / "manifest.json").read_text(encoding="ascii")
    manifest = json.loads(text)
    if key_vault:
        if not re.fullmatch(r"https://[a-z0-9-]{3,24}\.vault\.azure\.net/", key_vault):
            raise SystemExit("--key-vault must look like https://<name>.vault.azure.net/")
        connector = manifest["agentConnectors"][0]["toolSource"]["remoteMcpServer"]
        connector["authorization"]["referenceId"] = key_vault
    out = DIST / f"infojoy-gtm-microsoft-{manifest['version']}.zip"
    with zipfile.ZipFile(out, "w") as archive:
        _add(archive, "manifest.json", (json.dumps(manifest, indent=2, ensure_ascii=True)
                                        + "\n").encode("ascii"))
        for name in ("color.png", "outline.png"):
            _add(archive, name, (ROOT / "assets" / name).read_bytes())
        for name in ("mcptools.json", "intro.md"):
            _add(archive, name, (ROOT / "microsoft" / name).read_bytes())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--demo-url", help="the reviewer-accessible demo video (ChatGPT)")
    parser.add_argument("--key-vault", help="the Azure Key Vault URI holding the OAuth client")
    args = parser.parse_args()
    if subprocess.run([sys.executable, str(ROOT / "scripts" / "check.py")]).returncode:
        print("check.py failed: nothing built")
        return 1
    DIST.mkdir(exist_ok=True)
    for path in (build_openai(args.demo_url), build_microsoft(args.key_vault)):
        with zipfile.ZipFile(path) as archive:
            print(f"{path.relative_to(ROOT)}: {', '.join(archive.namelist())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
