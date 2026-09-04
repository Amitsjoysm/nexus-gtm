# tests/test_connection_budget.py
"""The fleet's connection demand must fit the database that has to serve it.

`config.py` already documents this hazard exactly, down to the rolling-deploy doubling:

    peak = app_replicas x processes x (pool + overflow + platform_pool + platform_overflow)
           x 2 during a rollout, + the worker's single process

Nothing computed it. Measured on the running deployment during a load test: 4 app processes and a
worker, each budgeted 35 connections, against a Postgres with `max_connections = 100` — 175
demanded, 1.8x overcommit. At 200 concurrent readers the pool exhausted and 4.81% of authenticated
requests returned HTTP 500 after a 30-second wait, with a p95 of 2.66s against a 500ms objective.

That is arithmetic, not a load characteristic. It reproduces on any deployment with these defaults
and the failure is silent until traffic arrives — which is precisely the class of thing a startup
check exists for.

`connection_budget()` makes the sum a value. The deployment declares its topology, and a
configuration that cannot be satisfied says so at boot rather than at peak.
"""
from __future__ import annotations

import pytest


def test_the_budget_matches_the_documented_formula():
    from nexus.core.config import connection_budget

    b = connection_budget(
        pool=10, overflow=20, platform_pool=2, platform_overflow=3,
        app_replicas=2, processes_per_replica=2, worker_processes=1,
    )
    # 35 per process. 4 app processes doubled by a rollout = 280, plus one worker = 315.
    assert b["per_process"] == 35
    assert b["app_processes"] == 4
    assert b["steady_state"] == 35 * 5
    assert b["during_rollout"] == 35 * 4 * 2 + 35


def test_the_deployment_that_failed_is_reported_as_unsatisfiable():
    """The exact configuration measured on the running stack."""
    from nexus.core.config import connection_budget

    b = connection_budget(
        pool=10, overflow=20, platform_pool=2, platform_overflow=3,
        app_replicas=2, processes_per_replica=2, worker_processes=1,
        max_connections=100,
    )
    assert b["fits"] is False
    assert b["steady_state"] == 175
    assert "max_connections" in b["explain"]


def test_a_sized_deployment_fits_with_rollout_headroom():
    """Headroom is against the ROLLOUT peak, not steady state — the old and new revisions run
    together for the length of a release, which is why a configuration reads fine for weeks and
    then fails during a deploy."""
    from nexus.core.config import connection_budget

    # 16 + 5 + 5 = 26 per process. A rollout runs 8 app processes plus the worker: 26 x 9 = 234,
    # inside the 240 usable after the operator reserve.
    b = connection_budget(
        pool=16, overflow=5, platform_pool=2, platform_overflow=3,
        app_replicas=2, processes_per_replica=2, worker_processes=1,
        max_connections=300,
    )
    assert b["fits"] is True, b["explain"]
    assert b["during_rollout"] == 234
    assert b["during_rollout"] <= 300 * 0.8


def test_a_reserve_is_held_back_for_operators():
    """psql, pg_dump, the backup job and the monitoring scrape all need connections, and they are
    needed most at exactly the moment the fleet is saturated."""
    from nexus.core.config import connection_budget

    b = connection_budget(
        pool=10, overflow=20, platform_pool=2, platform_overflow=3,
        app_replicas=2, processes_per_replica=2, worker_processes=1,
        max_connections=316,        # exactly the rollout peak, no reserve
    )
    assert b["fits"] is False, "a configuration with zero operator headroom was accepted"


def test_sqlite_is_not_budgeted():
    """The pool settings do not apply, and a test run must not be refused for a limit that has
    no meaning on a file database."""
    from nexus.core.config import connection_budget

    b = connection_budget(
        pool=10, overflow=20, platform_pool=2, platform_overflow=3,
        app_replicas=2, processes_per_replica=2, worker_processes=1,
        max_connections=None,
    )
    assert b["fits"] is True
    assert b["max_connections"] is None


def test_the_running_settings_declare_a_topology():
    """The budget is only computable if the deployment says how many processes it runs. Defaults
    describe the shipped compose file so a deployment that changes neither is still checked."""
    from nexus.core.config import Settings

    s = Settings()
    assert s.app_replicas >= 1
    assert s.processes_per_replica >= 1
    assert s.db_max_connections is None or s.db_max_connections > 0
