#!/usr/bin/env python
"""Build the reviewable/deployable artifact: release/nexus-gtm-<rev>-artifact.zip

Same safety property as ``package_release.sh`` — the contents come from ``git ls-files``, so the
zip can only ever hold tracked source at HEAD. ``.env`` is gitignored and cannot appear; neither
can ``node_modules``, a local database, or the code-review graph.

What this adds is the filtering. The full tree carries 1.6 MB of internal planning material under
``docs/superpowers/`` — milestone plans, design memos, an audit dossier — that is written for the
team and means nothing to someone deploying or reviewing the build. Operational documentation is
KEPT, because an artifact you cannot deploy from is not a deliverable: the runbooks, the
deployment guides, the go-live checklist and the billing specs all ship.

Tests ship too. They are the evidence for every behavioural claim the code makes, and a reviewer
who cannot run them has to take the implementation's word for itself.

    python scripts/package_artifact.py [--out DIR]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

# Internal-only material: planning, pitch, and strategy documents. Excluded because they are
# neither needed to run the product nor to review it, and they are most of the doc weight.
EXCLUDE_PREFIXES = (
    "docs/superpowers/",
    ".code-review-graph/",
    ".claude/",
    ".audit/",
    "release/",
)
EXCLUDE_EXACT = {
    "docs/PITCH_DECK.md",
    "docs/SWOT-ANALYSIS.md",
    "docs/pricing-memo.html",
    "nexus.db",
    "rated_caps.txt",
}
# Never shipped whatever git thinks: belt and braces around the secret rule.
EXCLUDE_NAMES = {".env", ".env.local", ".env.production"}


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    )
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def keep(path: str) -> bool:
    if any(path.startswith(p) for p in EXCLUDE_PREFIXES):
        return False
    if path in EXCLUDE_EXACT:
        return False
    if Path(path).name in EXCLUDE_NAMES:
        return False
    if path.endswith((".pyc", ".pyo")):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="release")
    args = ap.parse_args()

    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, check=True
        ).stdout.decode().strip()
    )
    rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, check=True
    ).stdout.decode().strip()

    files = tracked_files(root)
    kept = [f for f in files if keep(f)]
    dropped = len(files) - len(kept)

    out_dir = root / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"nexus-gtm-{rev}-artifact.zip"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in kept:
            src = root / rel
            if src.is_file():
                z.write(src, arcname=f"nexus-gtm/{rel}")

    # A secret in the artifact is the one failure that cannot be walked back, so assert rather
    # than trust the filter above.
    with zipfile.ZipFile(target) as z:
        names = z.namelist()
        leaked = [n for n in names if Path(n).name in EXCLUDE_NAMES]
        if leaked:
            target.unlink()
            print(f"REFUSED: secret-bearing files in artifact: {leaked}", file=sys.stderr)
            return 1

    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"artifact : {target.relative_to(root)}")
    print(f"revision : {rev}")
    print(f"files    : {len(kept)} kept, {dropped} internal-only excluded")
    print(f"size     : {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
