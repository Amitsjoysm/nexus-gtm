#!/usr/bin/env python
"""Mirror ``distribution/`` into the public listing repository's working tree.

    python scripts/export_listing_repo.py --to "C:/Users/<you>/Projects/infojoy-gtm-mcp"

``distribution/`` is the source of truth: ``tests/test_mcp_distribution.py`` checks it against the
server code (tool names, the server address, the Microsoft tool file). This copies it to the target
folder, a separate git repository, with LF line endings, and removes files there that no longer
exist here. It never touches the target's ``.git``, never commits and never pushes: read
``git status`` there, commit, and push only with the owner's go-ahead.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "distribution"
SKIP = {".git", "dist", "__pycache__"}
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip"}


def _files(base: pathlib.Path) -> set[pathlib.PurePosixPath]:
    return {pathlib.PurePosixPath(p.relative_to(base).as_posix()) for p in base.rglob("*")
            if p.is_file() and not SKIP & set(p.relative_to(base).parts)}


def export(target: pathlib.Path) -> tuple[list[str], list[str]]:
    if target.exists() and any(target.iterdir()) and not (target / ".git").is_dir():
        raise SystemExit(f"{target} is not empty and not a git repository; refusing to mirror")
    target.mkdir(parents=True, exist_ok=True)
    wanted, written = _files(SOURCE), []
    for rel in sorted(wanted):
        data = (SOURCE / rel).read_bytes()
        if (SOURCE / rel).suffix.lower() not in BINARY:
            data = data.replace(b"\r\n", b"\n")
        out = target / rel
        if not out.is_file() or out.read_bytes() != data:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            written.append(str(rel))
    removed = []
    for rel in sorted(_files(target) - wanted):
        (target / rel).unlink()
        removed.append(str(rel))
    return written, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--to", required=True, type=pathlib.Path,
                        help="the listing repository's folder")
    args = parser.parse_args()
    written, removed = export(args.to.resolve())
    for rel in written:
        print("wrote  ", rel)
    for rel in removed:
        print("removed", rel)
    print(f"{len(written)} written, {len(removed)} removed; nothing committed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
