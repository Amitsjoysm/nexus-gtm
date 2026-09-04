# tests/test_backup_scripts.py
"""The backup scripts must name things that exist.

Found by running `scripts/backup_db.sh` for the first time during a production readiness audit.
It defaulted to `PG_SERVICE=db`; the compose service is `postgres`. The script had been in the
repository since July, the audit cited its existence as evidence that backups were covered, and
it had never once produced a dump. There were zero backups on disk.

The failure mode is what makes this worth a test rather than a fix:

    $COMPOSE exec -T "$PG_SERVICE" pg_dump ... > "$OUT"

The shell creates `$OUT` before `pg_dump` runs, so a failure left `nexus_<timestamp>.dump` at
**0 bytes** — a correct-looking name in a correct-looking directory, indistinguishable from a real
backup to anyone running `ls`, and counted by the retention sweep as one of the seven kept. A
missing backup is a visible problem. A backup-shaped file that cannot be restored is the one that
gets discovered during an incident.

These tests read the scripts as text because there is no shell test runner here, in the same
spirit as the tests that read frontend source.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DB = ROOT / "scripts" / "backup_db.sh"
BACKUP_CRON = ROOT / "scripts" / "backup_cron.sh"
COMPOSE = ROOT / "deploy" / "docker-compose.prod.yml"


def _compose_services() -> set[str]:
    """Top-level service names, read without a YAML dependency."""
    out, in_services = set(), False
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services:
            if re.match(r"^[A-Za-z_]", line):      # a new top-level key ends the block
                break
            m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
            if m:
                out.add(m.group(1))
    return out


def _default_of(script: Path, var: str) -> str | None:
    m = re.search(rf'^{var}="\$\{{{var}:-([^}}"]+)\}}"', script.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_the_backup_targets_a_service_that_exists():
    """The bug. A default naming no real service fails on every scheduled run, forever."""
    services = _compose_services()
    assert services, "could not parse any service from the production compose file"

    target = _default_of(BACKUP_DB, "PG_SERVICE")
    assert target is not None, "backup_db.sh no longer declares a PG_SERVICE default"
    assert target in services, (
        f"backup_db.sh backs up compose service {target!r}, which does not exist. "
        f"Services are: {sorted(services)}"
    )


def test_the_dump_is_not_written_straight_to_its_final_name():
    """A shell redirect creates the file before the command runs, so a failure leaves a 0-byte
    file wearing a real backup's name."""
    body = BACKUP_DB.read_text(encoding="utf-8")
    assert 'PARTIAL="${OUT}.partial"' in body, "backup_db.sh no longer stages through a partial"
    assert re.search(r'pg_dump[^\n]*>\s*"\$PARTIAL"', body), (
        "pg_dump writes somewhere other than the staging file"
    )
    assert not re.search(r'pg_dump[^\n]*>\s*"\$OUT"', body), (
        "pg_dump writes directly to the final name — a failed run leaves a 0-byte dump"
    )
    assert re.search(r'mv "\$PARTIAL" "\$OUT"', body), "the verified dump is never moved into place"


def test_a_dump_is_verified_before_it_counts_as_a_backup():
    """Size alone does not prove restorability; `pg_restore --list` parses the archive TOC."""
    body = BACKUP_DB.read_text(encoding="utf-8")
    assert "pg_restore --list" in body, "nothing checks the dump is a readable archive"
    assert re.search(r'if \[ "\$SIZE" -lt \d+ \]', body), "nothing rejects an implausibly small dump"


def test_the_cron_wrapper_can_find_the_compose_file():
    """Cron runs from the repo root, where a bare `docker compose` finds no compose file."""
    default = _default_of(BACKUP_CRON, "COMPOSE")
    assert default and "docker-compose.prod.yml" in default, (
        f"backup_cron.sh COMPOSE default is {default!r}; cron would not find the stack"
    )
    assert "export COMPOSE" in BACKUP_CRON.read_text(encoding="utf-8"), (
        "COMPOSE is not exported, so backup_db.sh falls back to its own default"
    )


def test_a_failed_backup_is_recorded_where_something_can_see_it():
    """Nobody reads a backup log until they need a restore, and by then the gap is however long
    it has been broken."""
    body = BACKUP_CRON.read_text(encoding="utf-8")
    assert "STATUS_FILE" in body, "no status file is written for monitoring to check"
    assert "trap on_error ERR" in body, "an unexpected failure records nothing"
    assert re.search(r'if \[ ! -s "\$DUMP" \]', body), (
        "the wrapper accepts an empty dump as a successful backup"
    )


@pytest.mark.parametrize("script", [BACKUP_DB, BACKUP_CRON])
def test_the_scripts_fail_loudly(script):
    assert "set -euo pipefail" in script.read_text(encoding="utf-8")
