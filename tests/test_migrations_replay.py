# tests/test_migrations_replay.py
"""The migration chain must be a reproducible description of the schema.

This guards a failure mode that hid for a long time: the old ``0001_initial`` called
``Base.metadata.create_all()``, so it materialized whatever models happened to be registered when
it ran rather than a frozen historical schema. It therefore drifted forward with every new model
and pre-created tables that later revisions then failed to create. Nobody noticed, because
``scripts/bootstrap_db.py`` bootstraps a fresh database with create_all + ``alembic stamp head``
and so never replays the chain.

The consequence was that the migration history could not rebuild the database — disaster
recovery from migrations alone, or standing up a fresh environment from the chain, would have
failed. This test builds a database from nothing but ``alembic upgrade head`` and asserts the
result matches the models exactly.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "NEXUS_DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO, env=env, capture_output=True, text=True,
    )


def test_chain_has_exactly_one_head(tmp_path):
    proc = _alembic(tmp_path / "heads.db", "heads")
    assert proc.returncode == 0, proc.stderr[-2000:]
    heads = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1, f"expected a single head, got: {heads}"


def test_alembic_chain_rebuilds_the_current_schema(tmp_path):
    """`alembic upgrade head` on an empty database must reproduce `Base.metadata` exactly."""
    from sqlalchemy import create_engine, inspect

    import nexus.models  # noqa: F401  (register all mappers)
    from nexus.core.db import Base

    db_path = tmp_path / "chain.db"
    proc = _alembic(db_path, "upgrade", "head")
    assert proc.returncode == 0, (
        "the migration chain no longer replays onto an empty database:\n"
        + proc.stderr[-4000:]
    )

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        insp = inspect(engine)
        built = set(insp.get_table_names()) - {"alembic_version"}
        expected = set(Base.metadata.tables)

        assert not (expected - built), (
            "tables in the models but not produced by the chain — a model was added without a "
            f"migration: {sorted(expected - built)}"
        )
        assert not (built - expected), (
            f"tables produced by the chain but absent from the models: {sorted(built - expected)}"
        )

        for name in sorted(expected):
            in_db = {c["name"] for c in insp.get_columns(name)}
            in_models = {c.name for c in Base.metadata.tables[name].columns}
            assert in_db == in_models, (
                f"{name}: columns missing from the chain {sorted(in_models - in_db)}, "
                f"unexpected {sorted(in_db - in_models)}"
            )
    finally:
        engine.dispose()
