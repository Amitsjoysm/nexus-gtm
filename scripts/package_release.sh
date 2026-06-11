#!/usr/bin/env bash
# Build the production release artifact: release/nexus-gtm-<version>.zip
#
# Uses `git archive`, so the zip contains exactly the tracked source tree at HEAD —
# no secrets (.env is gitignored), no node_modules, no build junk. Unzip on a VM and
# run deploy/deploy.sh <domain> to go live.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

VERSION="$(git describe --tags --always 2>/dev/null || git rev-parse --short HEAD)"
OUT="release/nexus-gtm-${VERSION}.zip"

mkdir -p release
git archive --format=zip --prefix="nexus-gtm/" -o "$OUT" HEAD
echo "release artifact: $OUT"
unzip -l "$OUT" | tail -2
