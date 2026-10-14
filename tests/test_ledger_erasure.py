"""Erasing a person from the training ledger really removes them, and keeps them out (2026-10-14).

Asked for as "ensure if an email is added every detail about a person is really deleted". Before:

* the outbox, which holds every event as written (email text included) until the archive takes it
  and for seven days after, was never touched, so a waiting event shipped the person straight back;
* only the contact an email was addressed to could be found: a Cc'd colleague or an address in a
  quoted thread could not be erased at all;
* nothing remembered the request, so the next event about them was collected again;
* the erasure needed the pseudonymisation secret, so before the ledger was set up (exactly when the
  outbox fills with nowhere to go) nobody could be erased.

And the stores could only be configured in Provider keys, which the Ledger tab never mentioned. The
same tab now takes the connection strings and generates the secret.

Real SQLite rows and real requests throughout. The Postgres half (archive, training and insights
rows, and the cascade through them) is in `tests_integration/test_ledger_stores_pg.py`.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from nexus.core.config import get_settings
from tests.conftest import assert_staff_surface_hidden, auth, make_tenant, signup, tenant_session

SECRET = "a-test-pseudonymisation-secret-with-enough-variety-0123456789"
PRIYA = "priya.colleague@acme.io"


@pytest.fixture(autouse=True)
def _no_ledger(monkeypatch):
    """No store, no secret, unless a test sets one."""
    from nexus.providers import resolver

    for field in ("ledger_archive_dsn", "ledger_training_dsn", "ledger_insights_dsn",
                  "ledger_pseudonym_secret"):
        monkeypatch.setattr(get_settings(), field, "")
    resolver.invalidate()
    yield
    resolver.invalidate()


async def _superadmin(client, monkeypatch, *, slug: str) -> str:
    email = f"boss@{slug}.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def _workspace_with_events(slug: str) -> str:
    """A consenting workspace with three waiting events: one to Jane that Cc's Priya, one to Jane
    alone, one to Bob. Returns the tenant id."""
    from nexus.engagement.ledger import consent
    from nexus.engagement.ledger.emit import emit
    from nexus.models.account import Account, Contact

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(account)
        await ts.flush()
        jane = Contact(account_id=account.id, full_name="Jane Buyer", email="jane.buyer@acme.io")
        bob = Contact(account_id=account.id, full_name="Bob Other", email="bob@acme.io")
        ts.add(jane)
        ts.add(bob)
        await ts.flush()
        await emit(ts, "message.sent", refs={"contact_id": jane.id},
                   payload={"subject": "hello", "body": f"Hi Jane,\nlooping in {PRIYA}."})
        await emit(ts, "message.sent", refs={"contact_id": jane.id},
                   payload={"subject": "hello", "body": "Hi Jane"})
        await emit(ts, "message.sent", refs={"contact_id": bob.id},
                   payload={"subject": "hello", "body": "Hi Bob"})
    return tid


async def _outbox(tid: str) -> list:
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.ledger import LedgerOutbox

    async with get_platform_sessionmaker()() as session:
        return list((await session.execute(
            select(LedgerOutbox).where(LedgerOutbox.tenant_id == tid))).scalars().all())


async def _run_queued(queue) -> dict:
    from nexus.workers.tasks import HANDLERS

    job = await queue.dequeue(timeout=0)
    assert job is not None and job.name == "ledger_erase_person"
    return await HANDLERS[job.name](job.payload)


# ---- who an event names ---------------------------------------------------------------------------

def test_an_event_names_its_contact_its_sender_and_every_address_written_in_it():
    from nexus.engagement.ledger.erasures import everyone

    envelope = {"payload": {"body": "Hi Jane,\npriya@acme.io and 'raj@acme.io'.",
                            "thread": [{"body": "> From: lee@acme.io"}]}}
    resolved = {"contact_email": "Jane.Buyer@Acme.io", "sdr_email": "sam@seller.com",
                "person_emails": ["jane.buyer@acme.io"]}
    names = everyone(envelope, resolved)
    assert names == ["jane.buyer@acme.io", "lee@acme.io", "priya@acme.io", "raj@acme.io",
                     "sam@seller.com"]
    # Searched string by string: a JSON dump would read the line break as "\\n" + "priya@...".
    assert "npriya@acme.io" not in names


def test_an_archive_row_is_keyed_by_everyone_it_names_not_only_the_contact():
    from nexus.engagement.ledger import envelope, shipper
    from nexus.engagement.ledger.pseudonym import person_key
    from tests.test_engagement_ledger_stores import NOW

    body = envelope.build(event_id="ev-1", event_type="message.sent", tenant_id="t-1",
                          occurred_at=NOW, payload={"body": f"cc {PRIYA}"})
    resolved = {"contact_email": "jane.buyer@acme.io", "person_emails": ["jane.buyer@acme.io"]}
    keys = shipper.archive_row(SECRET, body, resolved)[-1]
    assert person_key(SECRET, PRIYA) in keys
    assert person_key(SECRET, "jane.buyer@acme.io") in keys


# ---- erasure ---------------------------------------------------------------------------------------

async def test_erasing_before_the_ledger_is_set_up_clears_the_outbox_and_is_remembered(
        client, monkeypatch):
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    tid = await _workspace_with_events("erase-early")
    rows = await _outbox(tid)
    assert len(rows) == 3
    # One already shipped: retention keeps it, with its text, for seven days.
    async with get_platform_sessionmaker()() as session:
        from nexus.models.ledger import LedgerOutbox

        cc = next(r for r in rows if PRIYA in json.dumps(r.payload))
        row = await session.get(LedgerOutbox, cc.id)
        row.shipped_archive_at = utcnow() - timedelta(days=1)
        await session.commit()

    token = await _superadmin(client, monkeypatch, slug="erase-early-adm")
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        r = await client.post("/api/admin/ledger/erase-person", json={"email": PRIYA},
                              headers=auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["person_key"] == ""            # no secret, so no ledger key
        job = await queue.dequeue(timeout=0)
        assert PRIYA not in json.dumps(job.payload) and "priya" not in json.dumps(job.payload)
        await queue.enqueue(job)
        report = await _run_queued(queue)
    finally:
        set_task_queue(None)

    assert report["deleted"]["outbox"] == 1 and report["remaining"]["outbox"] == 0
    left = await _outbox(tid)
    assert len(left) == 2 and not any(PRIYA in json.dumps(r.payload) for r in left)

    status = (await client.get("/api/admin/ledger", headers=auth(token))).json()
    erasure = status["recent_erasures"][0]
    assert erasure["state"] == "done" and erasure["remaining"]["outbox"] == 0
    assert "no pseudonymisation secret" in erasure["note"]
    assert PRIYA not in json.dumps(status)


async def test_the_contact_an_email_was_sent_to_is_erased_from_every_waiting_event(
        client, monkeypatch):
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    tid = await _workspace_with_events("erase-contact")
    token = await _superadmin(client, monkeypatch, slug="erase-contact-adm")
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        assert (await client.post("/api/admin/ledger/erase-person",
                                  json={"email": "JANE.BUYER@acme.io"},
                                  headers=auth(token))).status_code == 200
        report = await _run_queued(queue)
    finally:
        set_task_queue(None)
    assert report["deleted"]["outbox"] == 2
    left = await _outbox(tid)
    assert len(left) == 1 and "Hi Bob" in json.dumps(left[0].payload)


async def test_an_erased_person_is_never_shipped_again(monkeypatch):
    """Events recorded AFTER the erasure are deleted by the shipper, never archived. Every row of
    this batch names the erased person, so it finishes without opening a store connection."""
    from nexus.engagement.ledger import erasures, shipper
    from nexus.providers import resolver

    tid = await _workspace_with_events("erase-later")
    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", SECRET)
    resolver.invalidate()
    await erasures.record_request("jane.buyer@acme.io", person_key="", user_id=None)
    await erasures.record_request("bob@acme.io", person_key="", user_id=None)
    result = await shipper.ship_batch()
    assert result == {"shipped": 0, "erased": 3}
    assert await _outbox(tid) == []


async def test_a_store_that_fails_leaves_the_erasure_open_and_the_job_raises(client, monkeypatch):
    from nexus.engagement.ledger import erasures
    from nexus.providers import resolver
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", SECRET)
    # Nothing listens on port 1: a real refusal, not a patched function.
    monkeypatch.setattr(get_settings(), "ledger_archive_dsn",
                        "postgresql://u:p@127.0.0.1:1/postgres")
    resolver.invalidate()
    token = await _superadmin(client, monkeypatch, slug="erase-fail-adm")
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        r = await client.post("/api/admin/ledger/erase-person", json={"email": PRIYA},
                              headers=auth(token))
        assert r.status_code == 200, r.text
        with pytest.raises(RuntimeError, match="archive"):
            await _run_queued(queue)
    finally:
        set_task_queue(None)
    row = await erasures.get(r.json()["erasure_id"])
    assert row.finished_at is None and "archive" in row.report["errors"]
    status = (await client.get("/api/admin/ledger", headers=auth(token))).json()
    assert status["recent_erasures"][0]["state"] == "incomplete"


async def test_erasing_a_shared_person_queues_keys_even_without_the_secret(monkeypatch):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.ledger import erasures
    from nexus.people.store import forget_person, resolve_person_record
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        async with get_platform_sessionmaker()() as session:
            person = await resolve_person_record(session, email=PRIYA, full_name="Priya C")
            await session.commit()
            assert await forget_person(session, person.id) is True
            await session.commit()
        job = await queue.dequeue(timeout=0)
        assert job.payload["erasure_key"] == erasures.erasure_key(PRIYA)
        assert "priya" not in json.dumps(job.payload).lower()
        await queue.enqueue(job)
        await _run_queued(queue)
    finally:
        set_task_queue(None)
    # The job wrote the erasure row the transaction above could not.
    assert erasures.erasure_key(PRIYA) in await erasures.blocked_keys()


# ---- configuring the stores from the Ledger tab ---------------------------------------------------

async def test_the_connection_strings_are_saved_sealed_and_never_shown(client, monkeypatch):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement import config
    from nexus.models.billing import BillingAuditLog

    member = await signup(client, slug="ledger-set-rep", email="rep@ledger-set-rep.com",
                          company="Rep")
    assert_staff_surface_hidden(await client.put("/api/admin/ledger/setup", json={},
                                                 headers=auth(member)))
    token = await _superadmin(client, monkeypatch, slug="ledger-set")
    dsn = "postgresql://nexus_ledger:hunter2@db.example.com:5432/archive"
    r = await client.put("/api/admin/ledger/setup", json={"archive_dsn": dsn},
                         headers=auth(token))
    assert r.status_code == 200, r.text
    assert "hunter2" not in r.text and "db.example.com" not in r.text
    archive = next(s for s in r.json()["stores"] if s["store"] == "archive")
    assert archive["configured"] is True and archive["source"] == "panel"
    assert archive["hint"] == dsn[-4:]
    assert await config.ledger_dsn("archive") == dsn

    # A blank box keeps what is stored.
    r = await client.put("/api/admin/ledger/setup", json={"archive_dsn": ""}, headers=auth(token))
    assert r.status_code == 200 and await config.ledger_dsn("archive") == dsn

    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(select(BillingAuditLog).where(
            BillingAuditLog.action == "ledger.setup"))).scalars().all()
    assert rows and "hunter2" not in json.dumps([r.after for r in rows], default=str)


@pytest.mark.parametrize(("dsn", "words"), [
    ("mysql://u:p@db.example.com/x", "only PostgreSQL"),
    ("postgresql://u:p@169.254.169.254:5432/postgres", "refusing to connect"),
])
async def test_a_connection_string_the_guard_refuses_is_not_saved(client, monkeypatch, dsn, words):
    from nexus.engagement import config

    monkeypatch.setattr(get_settings(), "env", "prod")
    monkeypatch.setattr(get_settings(), "source_db_allow_private", False)
    token = await _superadmin(client, monkeypatch, slug="ledger-bad")
    r = await client.put("/api/admin/ledger/setup", json={"training_dsn": dsn},
                         headers=auth(token))
    assert r.status_code == 400 and words in r.json()["detail"], r.text
    assert await config.ledger_dsn("training") == ""


async def test_the_secret_is_generated_once_and_never_replaced_from_the_tab(client, monkeypatch):
    from nexus.engagement import config

    token = await _superadmin(client, monkeypatch, slug="ledger-secret")
    r = await client.put("/api/admin/ledger/setup", json={"generate_pseudonym_secret": True},
                         headers=auth(token))
    assert r.status_code == 200 and r.json()["pseudonym_secret_configured"] is True
    secret = await config.pseudonym_secret()
    assert len(secret) >= 32 and secret not in r.text
    again = await client.put("/api/admin/ledger/setup", json={"generate_pseudonym_secret": True},
                             headers=auth(token))
    assert again.status_code == 409 and "unreadable" in again.json()["detail"]
    assert await config.pseudonym_secret() == secret


def test_the_ledger_tab_offers_the_setup_and_the_erasure_history():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    tab = (src / "pages/admin/LedgerTab.tsx").read_text(encoding="utf-8")
    api = (src / "lib/api.ts").read_text(encoding="utf-8")
    assert "api.saveLedgerSetup" in tab and '"/admin/ledger/setup"' in api
    for words in ("Save connection strings", "Generate secret", "recent_erasures"):
        assert words in tab
