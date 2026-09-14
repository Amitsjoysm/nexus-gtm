# tests/test_bootstrap_db.py
"""``scripts/bootstrap_db.py`` against a database that is AHEAD of the image.

The production Rollback stage (``azure-pipelines-cd.yml``) redeploys the PREVIOUS image after a
failed release. The app migrates on boot, so if the failed release got far enough to apply a new
revision, ``alembic_version`` now names a revision the older image's ``migrations/versions/`` does
not contain. ``alembic upgrade head`` then dies with "Can't locate revision identified by ...",
the entrypoint (``set -e``) exits, and the rolled-back app crash-loops — against a schema it could
serve perfectly well, because migrations are additive.

These run the script the way the entrypoint does: a separate process with its own
``NEXUS_DATABASE_URL``. In-process, the engine and settings are pinned to the suite's database.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

REPO = Path(__file__).resolve().parents[1]
FUTURE_REVISION = "9999_from_a_newer_release"


def _stamped_database(db_path: Path, revision: str) -> None:
    """A fully built schema whose ``alembic_version`` names ``revision`` — a deployed database."""
    import nexus.models  # noqa: F401  (register all mappers)
    from nexus.core.db import Base

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            ))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": revision}
            )
    finally:
        engine.dispose()


def _versions(db_path: Path) -> list[str]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.connect() as conn:
            return list(conn.scalars(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()


def _bootstrap(db_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "NEXUS_DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
        # This checkout's `nexus`, not whichever copy happens to be pip-installed (-e) elsewhere.
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")])),
    }
    return subprocess.run(
        [sys.executable, "scripts/bootstrap_db.py"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )


def test_database_ahead_of_the_image_boots_instead_of_crash_looping(tmp_path):
    db = tmp_path / "ahead.db"
    _stamped_database(db, FUTURE_REVISION)

    proc = _bootstrap(db)

    assert proc.returncode == 0, (
        "a rolled-back image must boot against a database a newer release already migrated:\n"
        + proc.stdout[-2000:] + proc.stderr[-4000:]
    )
    assert "database is at a newer revision than this image" in proc.stdout
    assert _versions(db) == [FUTURE_REVISION]  # neither downgraded nor re-stamped


def test_a_genuine_migration_failure_still_fails_the_boot(tmp_path):
    """The skip is for a revision this image does not know. A known one that fails must stop it."""
    from alembic.script import ScriptDirectory

    (base,) = ScriptDirectory(str(REPO / "migrations")).get_bases()
    db = tmp_path / "broken.db"
    # Today's schema stamped at the FIRST revision: the upgrade replays every later revision onto
    # tables that already exist, and the first CREATE TABLE fails — a real migration error.
    _stamped_database(db, base)

    proc = _bootstrap(db)

    assert proc.returncode != 0, "a failing migration must fail the boot, not be skipped"
    assert "stamped database: alembic upgrade head" in proc.stdout
    assert "already exists" in proc.stderr, proc.stderr[-4000:]  # failed IN the migration
    assert "newer revision" not in proc.stdout
