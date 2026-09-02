"""M11 — job durability: retry with backoff, dead-lettering, and admin replay.

The defect this guards: ``dispatch`` swallowed every handler exception and the worker loop
logged it and moved on, so a failed ``process_account`` / campaign send / orchestration run was
simply *gone*. Periodic sweeps self-healed (re-enqueued each tick, idempotent); one-shot jobs
did not. That is silent, customer-visible data loss.

Everything here runs on the in-memory queue with retry delays pinned to zero so the tests are
deterministic and never sleep.
"""
from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import get_settings
from nexus.workers.queue import InMemoryTaskQueue, Job


# ---------------------------------------------------------------- Job envelope compatibility

def test_job_deserializes_the_old_two_field_shape():
    """A job serialized by the OLD code is still in Valkey when the new code deploys.

    If ``from_json`` demanded the new keys, every in-flight job would blow up on the first
    dequeue after a rolling restart — the upgrade itself would lose work.
    """
    job = Job.from_json('{"name": "process_account", "payload": {"tenant_id": "t1"}}')

    assert job.name == "process_account"
    assert job.payload == {"tenant_id": "t1"}
    assert job.attempts == 0
    assert job.max_attempts == 3


def test_job_roundtrips_attempt_counters_through_json():
    """The retry counter has to survive the queue, or a job retries forever."""
    job = Job(name="run_campaign", payload={"campaign_id": "c1"}, attempts=2, max_attempts=5)

    revived = Job.from_json(job.to_json())

    assert revived.attempts == 2
    assert revived.max_attempts == 5
    assert revived.name == "run_campaign"
    assert revived.payload == {"campaign_id": "c1"}


def test_old_code_can_still_read_a_new_job_envelope():
    """Forward compatibility during a rolling deploy: an old worker reads name/payload and
    ignores what it does not know. Assert the wire format keeps those keys at the top level."""
    import json

    raw = json.loads(Job(name="rollup_usage", payload={"a": 1}, attempts=1).to_json())

    assert raw["name"] == "rollup_usage"
    assert raw["payload"] == {"a": 1}


def test_defaults_match_a_job_built_the_old_way():
    """Every existing ``Job(name=..., payload=...)`` call site keeps compiling and behaving."""
    job = Job(name="advance_cadences", payload={})

    assert job.attempts == 0
    assert job.max_attempts == 3


# ------------------------------------------------------------------------ dispatch signalling

@pytest.mark.asyncio
async def test_dispatch_returns_handler_result_unchanged_on_success(monkeypatch):
    """The SUCCESS contract is untouched: whatever the handler returns is what comes back."""
    from nexus.workers import tasks as tasks_mod

    async def ok(payload: dict) -> dict:
        return {"status": "done", "error": "account_not_found"}

    monkeypatch.setitem(tasks_mod.HANDLERS, "m11_ok", ok)
    result = await tasks_mod.dispatch(Job(name="m11_ok", payload={}))

    assert result == {"status": "done", "error": "account_not_found"}
    # A handler that *returns* an "error" key is a normal outcome, not a crash. It must not be
    # mistaken for a failure, or `account_not_found` would be retried three times forever.
    assert not tasks_mod.is_job_failure(result)


@pytest.mark.asyncio
async def test_dispatch_surfaces_a_raised_handler_exception(monkeypatch):
    """The defect itself: an exception used to vanish into a log line."""
    from nexus.workers import tasks as tasks_mod

    async def boom(payload: dict) -> dict:
        raise RuntimeError("upstream 503")

    monkeypatch.setitem(tasks_mod.HANDLERS, "m11_boom", boom)
    result = await tasks_mod.dispatch(Job(name="m11_boom", payload={}))

    assert tasks_mod.is_job_failure(result)
    # The pre-existing shape is preserved for anything already reading `error`.
    assert "RuntimeError: upstream 503" in result["error"]


@pytest.mark.asyncio
async def test_unknown_job_is_not_a_retryable_failure():
    """An unroutable name will never route on the next attempt either — retrying it is a
    guaranteed-useless spin that would just re-enqueue itself until it dead-letters."""
    from nexus.workers import tasks as tasks_mod

    result = await tasks_mod.dispatch(Job(name="m11_does_not_exist", payload={}))

    assert result == {"error": "unknown_job", "name": "m11_does_not_exist"}
    assert not tasks_mod.is_job_failure(result)


# ------------------------------------------------------------------------------------ backoff

def test_backoff_grows_exponentially_and_is_capped():
    from nexus.workers.durability import compute_backoff

    delays = [compute_backoff(n, base_s=2.0, max_s=60.0) for n in (1, 2, 3, 4, 5, 6, 7)]

    # Monotonic up to the cap, and never above it.
    assert delays[0] < delays[1] < delays[2]
    assert all(d <= 60.0 for d in delays)
    assert delays[-1] > 30.0  # saturated at the ceiling rather than still doubling


def test_backoff_is_jittered_so_retries_do_not_stampede():
    """A provider outage fails a thousand jobs at once. Undithered backoff retries all thousand
    at the same instant and re-DDoSes the thing that just recovered."""
    from nexus.workers.durability import compute_backoff

    samples = {compute_backoff(3, base_s=2.0, max_s=60.0) for _ in range(50)}

    assert len(samples) > 1


def test_backoff_of_zero_base_is_immediate():
    """Tests pin the base delay to zero; that must mean 'now', not 'a jittered small sleep'."""
    from nexus.workers.durability import compute_backoff

    assert compute_backoff(1, base_s=0.0, max_s=60.0) == 0.0


# ------------------------------------------------------------------- retry through the worker

@pytest.fixture
def instant_retries(monkeypatch):
    """Zero backoff so the worker loop retries without sleeping."""
    monkeypatch.setattr(get_settings(), "job_retry_base_delay_s", 0.0)
    monkeypatch.setattr(get_settings(), "job_retry_max_delay_s", 0.0)


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_a_handler_that_fails_twice_then_succeeds_is_retried_to_completion(
    monkeypatch, instant_retries, fresh_db
):
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    attempts: list[int] = []
    done = asyncio.Event()

    async def flaky(payload: dict) -> dict:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        done.set()
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "m11_flaky", flaky)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="m11_flaky", payload={}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01))
    try:
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert len(attempts) == 3


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_a_permanently_failing_job_dead_letters_exactly_once(
    monkeypatch, instant_retries, fresh_db
):
    """Acceptance: after ``max_attempts`` the payload is preserved for a human, once."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    calls: list[int] = []

    async def always_fails(payload: dict) -> dict:
        calls.append(1)
        raise RuntimeError("permanently broken")

    monkeypatch.setitem(tasks_mod.HANDLERS, "m11_doomed", always_fails)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="m11_doomed", payload={"tenant_id": "t1", "account_id": "a1"}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01))
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 3:
                break
        await asyncio.sleep(0.1)  # let the dead-letter write land
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    assert len(calls) == 3, "attempted more (or fewer) times than max_attempts"

    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(DeadLetterJob))).all()
    assert len(rows) == 1
    assert rows[0].job_name == "m11_doomed"
    assert rows[0].payload == {"tenant_id": "t1", "account_id": "a1"}
    assert rows[0].attempts == 3
    assert "permanently broken" in rows[0].error
    assert rows[0].subject_tenant_id == "t1"
    assert rows[0].replayed_at is None


@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_a_job_that_succeeds_first_time_never_touches_the_dead_letter_table(
    monkeypatch, instant_retries, fresh_db
):
    """Hard rule: the in-memory path used by the suite behaves exactly as it does today when a
    job succeeds on the first attempt — no extra enqueues, no extra rows."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    ran = asyncio.Event()

    async def fine(payload: dict) -> dict:
        ran.set()
        return {"ok": True}

    monkeypatch.setitem(tasks_mod.HANDLERS, "m11_fine", fine)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="m11_fine", payload={}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01))
    try:
        await asyncio.wait_for(ran.wait(), timeout=10)
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    async with get_sessionmaker()() as session:
        assert (await session.scalars(select(DeadLetterJob))).all() == []
    assert await q.dequeue(timeout=0) is None  # nothing re-enqueued


# ------------------------------------------------------------------------------------ counters

@pytest.mark.asyncio
async def test_counters_track_enqueued_and_outcomes(monkeypatch, instant_retries, fresh_db):
    from nexus.workers import metrics as metrics_mod
    from nexus.workers.durability import record_job_failure

    metrics_mod.reset_job_counters()
    q = InMemoryTaskQueue()
    await q.enqueue(Job(name="m11_counted", payload={}))
    assert metrics_mod.job_counters()["enqueued"] == 1

    metrics_mod.increment_job_counter("succeeded")
    assert metrics_mod.job_counters()["succeeded"] == 1

    # One retry, then the dead letter.
    await record_job_failure(q, Job(name="m11_counted", payload={}, attempts=0), "boom")
    assert metrics_mod.job_counters()["retried"] == 1
    await record_job_failure(q, Job(name="m11_counted", payload={}, attempts=2), "boom")
    assert metrics_mod.job_counters()["dead_lettered"] == 1


# ------------------------------------------------------------------------------- dedupe / decay

@pytest.mark.asyncio
async def test_the_same_job_failing_repeatedly_updates_one_row(instant_retries, fresh_db):
    """A broken upstream fails the same job every tick. That is one incident, not ten thousand
    rows — otherwise the triage list is unusable exactly when it matters most."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob
    from nexus.workers.durability import record_job_failure

    q = InMemoryTaskQueue()
    payload = {"tenant_id": "t1", "account_id": "a1"}
    for _ in range(3):
        await record_job_failure(q, Job(name="m11_repeat", payload=payload, attempts=2), "boom")

    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(DeadLetterJob))).all()
    assert len(rows) == 1
    assert rows[0].last_seen_at >= rows[0].first_seen_at


@pytest.mark.asyncio
async def test_disabling_retries_still_preserves_the_job(monkeypatch, fresh_db):
    """The kill switch turns off the RETRY, never the record: switching it off must degrade to
    'fail fast, keep the evidence', not back to losing work."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob
    from nexus.workers.durability import record_job_failure

    monkeypatch.setattr(get_settings(), "job_retry_enabled", False)
    q = InMemoryTaskQueue()

    outcome = await record_job_failure(q, Job(name="m11_noretry", payload={}), "boom")

    assert outcome == "dead_lettered"
    assert await q.dequeue(timeout=0) is None  # not retried
    async with get_sessionmaker()() as session:
        assert len((await session.scalars(select(DeadLetterJob))).all()) == 1


# ------------------------------------------------------------------- shutdown must not lose work

@pytest.mark.timeout(0)
@pytest.mark.asyncio
async def test_shutdown_flushes_jobs_that_are_mid_backoff(monkeypatch, fresh_db):
    """A job asleep in its backoff has already been REMOVED from the queue.

    Deploying (or restarting) during an incident is exactly when jobs are mid-backoff, so a
    worker that exits without putting them back loses precisely the work that was already
    struggling. The retry delay here is deliberately long: the worker must not wait it out.
    """
    from nexus.workers import tasks as tasks_mod
    from nexus.workers.worker import run_worker

    monkeypatch.setattr(get_settings(), "job_retry_base_delay_s", 30.0)
    monkeypatch.setattr(get_settings(), "job_retry_max_delay_s", 30.0)

    failed = asyncio.Event()

    async def boom(payload: dict) -> dict:
        failed.set()
        raise RuntimeError("transient")

    monkeypatch.setitem(tasks_mod.HANDLERS, "m11_slow_retry", boom)
    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    await q.enqueue(Job(name="m11_slow_retry", payload={"tenant_id": "t1"}))

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop=stop, poll_timeout=0.01))
    await asyncio.wait_for(failed.wait(), timeout=10)
    stop.set()
    await asyncio.wait_for(task, timeout=10)  # returns promptly, does not sleep out the backoff

    # The job is back on the queue, carrying its attempt count, ready for the next worker.
    requeued = await q.dequeue(timeout=0)
    assert requeued is not None, "a job mid-backoff was lost on shutdown"
    assert requeued.name == "m11_slow_retry"
    assert requeued.attempts == 1


# --------------------------------------------------------------------------- RLS posture guard

def test_dead_letter_table_is_not_tenant_scoped():
    """The CLAUDE.md trap, asserted rather than remembered.

    ``scripts/apply_rls.py`` enrols every table carrying a ``tenant_id`` column into Row-Level
    Security. Both parties who touch this table arrive without a tenant binding — the worker
    writing a dead letter mid-sweep, and the platform operator triaging it — and under RLS they
    would get ZERO ROWS rather than an error. Silent, and therefore the worst kind.
    """
    from nexus.models.jobs import DeadLetterJob

    assert "tenant_id" not in DeadLetterJob.__table__.columns
    assert "subject_tenant_id" in DeadLetterJob.__table__.columns


def test_apply_rls_does_not_enrol_the_dead_letter_table():
    """Assert it against the real selector, not against our reading of it."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "apply_rls.py"
    spec = importlib.util.spec_from_file_location("_apply_rls_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "dead_letter_jobs" not in module._tenant_tables()


# ------------------------------------------------------------------------------------ admin API

async def _make_dead_letter(job_name: str = "process_account", payload: dict | None = None) -> str:
    """Produce a dead letter through the real failure path, not a hand-written INSERT."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.jobs import DeadLetterJob
    from nexus.workers.durability import record_job_failure

    job = Job(name=job_name, payload=payload or {"tenant_id": "t1", "account_id": "a1"},
              attempts=2)
    await record_job_failure(InMemoryTaskQueue(), job, "RuntimeError: upstream 503")
    async with get_sessionmaker()() as session:
        row = (await session.scalars(select(DeadLetterJob))).all()[-1]
        return row.id


async def test_dead_letters_reject_a_tenant_owner(client):
    """Tenant RBAC grants nothing on the platform surface — a workspace owner is not staff."""
    from tests.conftest import auth, signup

    token = await signup(client, slug="dlq1", email="o@dlq1.com", company="DLQ1")
    r = await client.get("/api/admin/jobs/dead-letters", headers=auth(token))
    assert r.status_code in (401, 404)


async def test_dead_letters_reject_anonymous(client):
    r = await client.get("/api/admin/jobs/dead-letters")
    assert r.status_code in (401, 404)


async def test_replay_rejects_a_tenant_owner(client):
    from tests.conftest import auth, signup

    token = await signup(client, slug="dlq2", email="o@dlq2.com", company="DLQ2")
    r = await client.post("/api/admin/jobs/dead-letters/whatever/replay", headers=auth(token))
    assert r.status_code in (401, 404)


async def test_platform_admin_lists_dead_letters(client, monkeypatch, instant_retries):
    from tests.conftest import auth, signup

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="dlq3", email="boss@nexus.com", company="DLQ3")
    await _make_dead_letter()

    r = await client.get("/api/admin/jobs/dead-letters", headers=auth(token))

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["job_name"] == "process_account"
    assert body[0]["attempts"] == 3
    assert body[0]["subject_tenant_id"] == "t1"
    assert body[0]["replayed_at"] is None
    assert body[0]["payload"] == {"tenant_id": "t1", "account_id": "a1"}


async def test_replay_re_enqueues_the_original_job_and_clears_it(
    client, monkeypatch, instant_retries
):
    """Acceptance: replay re-runs the job and the dead letter stops showing up in triage."""
    from nexus.workers.queue import set_task_queue
    from tests.conftest import auth, signup

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="dlq4", email="boss@nexus.com", company="DLQ4")
    dead_id = await _make_dead_letter()

    q = InMemoryTaskQueue()
    set_task_queue(q)
    try:
        r = await client.post(f"/api/admin/jobs/dead-letters/{dead_id}/replay", headers=auth(token))
        assert r.status_code == 200, r.text

        replayed = await q.dequeue(timeout=0)
        assert replayed is not None
        assert replayed.name == "process_account"
        assert replayed.payload == {"tenant_id": "t1", "account_id": "a1"}
        # A replay is a fresh start: it gets the full attempt budget again, or a job that
        # dead-lettered once could never survive a second transient failure.
        assert replayed.attempts == 0
    finally:
        set_task_queue(None)

    # Gone from the default (open-only) triage list, still retrievable with the flag.
    assert await client.get("/api/admin/jobs/dead-letters", headers=auth(token)) is not None
    open_list = (await client.get("/api/admin/jobs/dead-letters", headers=auth(token))).json()
    assert open_list == []
    all_list = (
        await client.get("/api/admin/jobs/dead-letters?include_replayed=true", headers=auth(token))
    ).json()
    assert len(all_list) == 1
    assert all_list[0]["replayed_at"] is not None


async def test_replaying_twice_is_refused(client, monkeypatch, instant_retries):
    """A double-clicked replay must not run a side-effectful job twice."""
    from nexus.workers.queue import set_task_queue
    from tests.conftest import auth, signup

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="dlq5", email="boss@nexus.com", company="DLQ5")
    dead_id = await _make_dead_letter()

    q = InMemoryTaskQueue()
    set_task_queue(q)
    try:
        first = await client.post(
            f"/api/admin/jobs/dead-letters/{dead_id}/replay", headers=auth(token)
        )
        second = await client.post(
            f"/api/admin/jobs/dead-letters/{dead_id}/replay", headers=auth(token)
        )
    finally:
        set_task_queue(None)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text


async def test_replay_of_an_unknown_id_is_404(client, monkeypatch):
    from tests.conftest import auth, signup

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="dlq6", email="boss@nexus.com", company="DLQ6")

    r = await client.post("/api/admin/jobs/dead-letters/nope/replay", headers=auth(token))
    assert r.status_code == 404


async def test_replay_is_audited(client, monkeypatch, instant_retries):
    """Answerability: re-running a customer's job is an admin mutation and must be on the record."""
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingAuditLog
    from nexus.workers.queue import set_task_queue
    from tests.conftest import auth, signup

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="dlq7", email="boss@nexus.com", company="DLQ7")
    dead_id = await _make_dead_letter()

    set_task_queue(InMemoryTaskQueue())
    try:
        r = await client.post(f"/api/admin/jobs/dead-letters/{dead_id}/replay", headers=auth(token))
        assert r.status_code == 200, r.text
    finally:
        set_task_queue(None)

    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(BillingAuditLog).where(BillingAuditLog.action == "job.dead_letter.replay")
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].target == dead_id
    assert rows[0].subject_tenant_id == "t1"
