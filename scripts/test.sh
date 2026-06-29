#!/usr/bin/env bash
# Fast, reliable test runner.
#
# Why this exists:
#   * Some dev machines have several Pythons on PATH where a bare `python` lacks the test deps
#     (pytest, sqlalchemy, …). This picks the first interpreter that can actually import pytest.
#   * Runs in parallel with pytest-xdist (`-n auto`) — the suite is ~hundreds of async HTTP tests,
#     so parallelism cuts wall time from ~25 min to a few. Per-worker SQLite files (see
#     tests/conftest.py) make this safe.
#   * Unbuffered output so progress streams live (a quiet, buffered run can look "stuck").
#   * A per-test timeout (configured in pyproject.toml) kills any accidental hang and reports it,
#     instead of blocking the whole run forever.
#
# Usage:
#   scripts/test.sh                      # whole suite, parallel
#   scripts/test.sh tests/test_x.py -k name -x   # extra args pass through
#   PYBIN="py -3.10" scripts/test.sh     # force a specific interpreter
#   XDIST=0 scripts/test.sh              # serial run (e.g. for clean -x debugging)
set -uo pipefail
cd "$(dirname "$0")/.."

pick_py() {
  if [ -n "${PYBIN:-}" ]; then echo "$PYBIN"; return; fi
  for c in "py -3.11" "py -3.10" python3.11 python3.10 python3 python; do
    if $c -c "import pytest" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo "python"
}

PY="$(pick_py)"
PAR="-n auto"
[ "${XDIST:-1}" = "0" ] && PAR=""

echo "Using interpreter: $PY ${PAR:+(parallel)}"
PYTHONUNBUFFERED=1 $PY -u -m pytest $PAR -p no:cacheprovider -q "$@"
