"""The ledger's three stores: keys, scrubbing, datasets, profiles, shipping, deletion (§18.2-§18.6).

Everything here runs offline. The pure half (keys, scrubber, dataset builders, profile maths, the
shipping schedule) is tested directly; the store I/O is tested against real Postgres in
`tests_integration/test_engagement_ledger_stores.py`, because a fake asyncpg would only prove the
fake agrees with itself.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from nexus.core.config import get_settings
from tests.conftest import (
    assert_staff_surface_hidden,
    auth,
    make_tenant,
    signup,
    tenant_session,
)

SECRET = "a-test-pseudonymisation-secret-with-enough-variety-0123456789"
NEXUS = pathlib.Path(__file__).resolve().parents[1] / "nexus"
SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"
SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"

NOW = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


def _document(event_type: str, payload: dict, refs: dict | None = None, **resolved) -> dict:
    """An archive document: the envelope as emitted, plus who it was about."""
    from nexus.engagement.ledger import envelope

    body = envelope.build(
        event_id=f"ev-{event_type}", event_type=event_type, tenant_id="t-1", occurred_at=NOW,
        refs=refs or {}, payload=payload,
    )
    base = {"tenant_id": "t-1", "consent_terms_version": "2026-09-17",
            "contact_email": "jane.buyer@acme.io", "contact_name": "Jane Buyer",
            "account_name": "Acme Robotics", "account_domain": "acme.io",
            "sdr_name": "Sam Rep", "sdr_email": "sam@seller.com",
            "person_emails": ["jane.buyer@acme.io"], "other_names": []}
    base.update(resolved)
    return {"envelope": body, "resolved": base}


# ---- keys and splits ------------------------------------------------------------------------------

def test_a_person_is_the_same_key_everywhere_and_a_different_one_per_kind():
    from nexus.engagement.ledger import pseudonym

    first = pseudonym.person_key(SECRET, "Jane.Buyer@Acme.io ")
    assert first == pseudonym.person_key(SECRET, "jane.buyer@acme.io")   # normalised
    assert first != pseudonym.person_key("another-secret-entirely-xxxxxxxxxx", "jane.buyer@acme.io")
    assert first != pseudonym.company_key(SECRET, "jane.buyer@acme.io")  # kind is in the input
    assert len(first) == 32
    with pytest.raises(ValueError):
        pseudonym.key(SECRET, "not-a-kind", "x")
    with pytest.raises(ValueError):
        pseudonym.person_key("", "jane@acme.io")


def test_a_workspace_lands_in_one_split_and_the_mix_is_roughly_80_10_10():
    from collections import Counter

    from nexus.engagement.ledger import pseudonym

    keys = [pseudonym.workspace_key(SECRET, f"tenant-{i}") for i in range(600)]
    splits = [pseudonym.split_for(key) for key in keys]
    assert splits == [pseudonym.split_for(key) for key in keys]      # deterministic
    counts = Counter(splits)
    assert 0.70 < counts["train"] / len(keys) < 0.90
    assert counts["val"] and counts["test"]


def test_the_archive_key_is_derived_from_the_same_secret_and_round_trips():
    from nexus.engagement.ledger import pseudonym

    sealed = pseudonym.archive_fernet(SECRET).encrypt(b'{"hello": "world"}')
    assert b"hello" not in sealed
    assert pseudonym.archive_fernet(SECRET).decrypt(sealed) == b'{"hello": "world"}'


# ---- the scrubber ---------------------------------------------------------------------------------

def test_known_people_companies_addresses_and_numbers_become_stable_placeholders():
    from nexus.engagement.ledger.scrub import Known, Scrubber

    scrubber = Scrubber(Known(people=["Jane Buyer", "Sam Rep"],
                              companies=["Acme Robotics", "acme.io"],
                              emails=["jane.buyer@acme.io"]))
    first = scrubber.text(
        "Hi Jane, Sam here from Acme Robotics — reply to jane.buyer@acme.io or call "
        "+1 (415) 555-0134. See https://acme.io/pricing.")
    assert "Jane" not in first and "Sam" not in first and "Acme" not in first
    assert "jane.buyer@acme.io" not in first and "415" not in first and "acme.io" not in first
    assert "[PERSON_" in first and "[COMPANY_" in first and "[PHONE_" in first
    # The same person is the same placeholder in the next text of the same example.
    second = scrubber.text("Jane Buyer replied.")
    assert second.split()[0] == first.split()[1].rstrip(",")


def test_an_iso_date_survives_but_a_stranger_s_address_does_not():
    from nexus.engagement.ledger.scrub import Known, Scrubber

    scrubber = Scrubber(Known())
    assert scrubber.text("Let's meet on 2026-10-05.") == "Let's meet on 2026-10-05."
    assert "@" not in scrubber.text("Try priya@othercorp.com instead")


def test_the_scrubber_walks_nested_values_and_leaves_non_text_alone():
    from nexus.engagement.ledger.scrub import Known, Scrubber

    scrubber = Scrubber(Known(people=["Jane Buyer"]))
    out = scrubber.value({"thread": [{"body": "Jane Buyer said yes", "at": "2026-09-18"}],
                          "count": 3, "ok": True})
    assert "Jane" not in out["thread"][0]["body"]
    assert out["count"] == 3 and out["ok"] is True


# ---- dataset builders -----------------------------------------------------------------------------

def test_a_sent_message_makes_a_training_example_a_feature_row_and_a_send_fact():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "message.sent",
        {"subject": "Quick question, Jane", "body": "Hi Jane, saw Acme Robotics is hiring.",
         "context_pack": "Account: Acme Robotics ...", "sent_local_hour": 9,
         "sent_local_weekday": 1, "kind": "step", "step_index": 0,
         "persona": {"title": "VP Engineering", "seniority": "vp"},
         "account": {"industry": "robotics", "employee_count": 200, "country": "US"},
         "personalisation_facts": ["hiring 12 engineers"], "mailbox_provider": "google"},
        refs={"message_id": "m-1", "contact_id": "c-1", "account_id": "a-1"},
    ), SECRET)

    kinds = {example.dataset for example in built.examples}
    assert kinds == {"sft_outreach_email", "tab_engagement"}
    sft = next(x for x in built.examples if x.dataset == "sft_outreach_email")
    assistant = sft.record["messages"][-1]["content"]
    assert "Jane" not in assistant and "Acme" not in assistant
    assert sft.record["labels"] == {"replied": False, "positive": False, "meeting": False}
    assert sft.consent_terms_version == "2026-09-17"
    tab = next(x for x in built.examples if x.dataset == "tab_engagement")
    # The facts themselves are free text about a named person; the count is the feature.
    assert tab.record["features"]["personalisation_facts"] == 1
    assert tab.record["features"]["local_hour"] == 9
    assert [f.fact_type for f in built.facts] == ["send"]
    assert built.facts[0].person_email == "jane.buyer@acme.io"
    assert built.facts[0].attrs["message_id"] == "m-1"


def test_the_pseudonymised_event_keeps_no_tenant_no_ids_and_no_names():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "message.sent", {"subject": "Hi", "body": "Hi Jane Buyer"},
        refs={"message_id": "m-1", "contact_id": "c-1", "account_id": "a-1"}), SECRET)
    event = built.event
    assert "tenant_id" not in event["body"]
    assert event["workspace_key"] != "t-1"
    assert event["body"]["refs"]["contact_id"] != "c-1"
    assert event["body"]["refs"]["message_id"] == "m-1"    # internal ids are not identities
    assert "Jane" not in json.dumps(event["body"])
    assert event["person_keys"] == [
        __import__("nexus.engagement.ledger.pseudonym", fromlist=["x"]).person_key(
            SECRET, "jane.buyer@acme.io")]


def test_a_reply_labels_the_message_it_answers_and_records_a_reply_fact():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "reply.received",
        {"inbound_kind": "human", "response_latency_s": 5400, "local_hour": 11,
         "local_weekday": 2, "body": "Sounds interesting"},
        refs={"message_id": "m-2", "answered_message_id": "m-1"}), SECRET)

    patched = {(p.dataset, p.values.get("replied")) for p in built.patches}
    assert patched == {("sft_outreach_email", True), ("tab_engagement", True)}
    assert all(p.example_id == datasets.example_id(p.dataset, "m-1") for p in built.patches)
    assert built.patches[0].values["response_latency_s"] == 5400
    assert [f.fact_type for f in built.facts] == ["reply"]
    assert built.facts[0].response_latency_s == 5400


def test_a_classification_becomes_a_labelled_example_and_marks_the_send_positive():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "reply.classified",
        {"category": "interested", "confidence": 0.91, "body": "Yes, let's talk",
         "thread": [{"direction": "out", "at": "2026-09-17", "body": "Hi Jane"}],
         "label_source": "ai"},
        refs={"message_id": "m-2", "answered_message_id": "m-1"}), SECRET)

    example = next(x for x in built.examples if x.dataset == "cls_reply")
    assert example.record["output"]["category"] == "interested"
    assert example.record["output"]["label_source"] == "ai"
    assert "Jane" not in json.dumps(example.record)
    assert all(p.values["positive"] is True for p in built.patches)
    assert built.fact_categories[0].category == "interested"


def test_an_sdr_correction_replaces_the_label_and_says_who_set_it():
    from nexus.engagement.ledger import datasets

    corrected = datasets.build(_document(
        "reply.corrected", {"ai_category": "interested", "sdr_category": "not_now",
                            "resolved_date": "2026-11-01"},
        refs={"message_id": "m-2", "answered_message_id": "m-1"}), SECRET)
    patch = next(p for p in corrected.patches if p.dataset == "cls_reply")
    assert patch.path == "output"
    assert patch.values == {"category": "not_now", "resolved_date": "2026-11-01",
                            "label_source": "sdr_corrected"}
    assert {p.values.get("positive") for p in corrected.patches if p.dataset != "cls_reply"} == {False}

    confirmed = datasets.build(_document(
        "reply.corrected", {"ai_category": "interested", "sdr_category": "interested"},
        refs={"message_id": "m-2"}), SECRET)
    assert confirmed.patches[0].values["label_source"] == "sdr_confirmed"


def test_an_sdr_edit_becomes_a_preference_pair_and_an_unedited_draft_does_not():
    from nexus.engagement.ledger import datasets

    edited = datasets.build(_document(
        "draft.edited",
        {"context_pack": "Account: Acme ...", "ai_subject": "Hi", "ai_body": "AI wrote this",
         "subject": "Hi", "body": "The SDR rewrote this", "edit_distance": 14},
        refs={"message_id": "m-1"}), SECRET)
    pair = next(x for x in edited.examples if x.dataset == "pref_outreach_email")
    assert "SDR rewrote" in pair.record["chosen"] and "AI wrote" in pair.record["rejected"]

    untouched = datasets.build(_document(
        "draft.edited", {"context_pack": "c", "ai_subject": "Hi", "ai_body": "Same",
                         "subject": "Hi", "body": "Same"}, refs={"message_id": "m-9"}), SECRET)
    assert untouched.examples == []


def test_a_meeting_labels_every_example_the_outcome_is_attributed_to():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "outcome.recorded", {"stage": "meeting", "meta": {"message_id": "m-1"}},
        refs={"contact_id": "c-1"}), SECRET)
    assert {p.dataset for p in built.patches} == {
        "sft_outreach_email", "tab_engagement", "sft_reply_response"}
    assert all(p.values == {"meeting": True} for p in built.patches)

    ignored = datasets.build(_document("outcome.recorded", {"stage": "replied"}), SECRET)
    assert ignored.patches == []


def test_an_ai_call_is_kept_for_distillation_with_its_model_and_cost():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "ai.call", {"agent": "messaging", "status": "ok", "inputs": {"account": "Acme Robotics"},
                    "output": "Hi Jane", "tokens": 812, "latency_ms": 2400}), SECRET)
    example = next(x for x in built.examples if x.dataset == "ai_calls")
    assert example.record["tokens"] == 812
    assert "Jane" not in json.dumps(example.record) and "Acme" not in json.dumps(example.record)


def test_an_event_about_nobody_still_becomes_a_training_event_but_no_fact():
    from nexus.engagement.ledger import datasets

    built = datasets.build(_document(
        "message.sent", {"subject": "s", "body": "b"}, person_emails=[], contact_email=""), SECRET)
    assert built.event is not None
    assert built.facts == []
    assert built.event["person_keys"] == []


# ---- profiles -------------------------------------------------------------------------------------

def _fact(fact_type: str, *, hour=None, weekday=None, latency=None, at=NOW, workspace="w1",
          attrs=None) -> dict:
    return {"fact_type": fact_type, "person_key": "pk", "company_domain": "acme.io",
            "workspace_key": workspace, "occurred_at": at, "local_hour": hour,
            "local_weekday": weekday, "response_latency_s": latency, "attrs": attrs or {}}


def test_a_profile_is_the_shape_of_the_replies_that_remain():
    from nexus.engagement.ledger import insights

    facts = [
        _fact("send"), _fact("send"), _fact("send"), _fact("send"),
        _fact("reply", hour=9, weekday=1, latency=3000, at=NOW - timedelta(days=3)),
        _fact("reply", hour=9, weekday=1, latency=9000, at=NOW - timedelta(days=2),
              workspace="w2"),
        _fact("out_of_office", at=NOW - timedelta(days=1), attrs={"until": "2026-09-25"}),
    ]
    profile = insights.person_profile("jane.buyer@acme.io", facts)
    assert profile["best_hour"] == 9 and profile["best_weekday"] == 1
    assert profile["median_response_s"] == 6000
    assert profile["reply_propensity"] == 0.5
    assert profile["sends"] == 4 and profile["replies"] == 2
    assert profile["workspace_count"] == 2
    assert profile["last_reply_band"] == "same_day"
    assert profile["ooo_periods"] == [{"from": "2026-09-17", "until": "2026-09-25"}]
    assert insights.person_profile("jane.buyer@acme.io", []) is None


def test_the_speed_band_is_the_only_pattern_below_the_threshold():
    from nexus.engagement.ledger import insights

    assert insights.speed_band(600) == "within_hour"
    assert insights.speed_band(40_000) == "same_day"
    assert insights.speed_band(400_000) == "within_week"
    assert insights.speed_band(4_000_000) == "longer"
    assert insights.speed_band(None) == ""


# ---- shipping -------------------------------------------------------------------------------------

def test_a_failed_row_waits_longer_each_time_up_to_an_hour():
    from nexus.engagement.ledger import shipper

    assert [shipper.retry_delay_minutes(n) for n in (0, 1, 2, 3, 10)] == [0, 2, 4, 8, 60]
    assert shipper.is_due(0, NOW, NOW) is True
    assert shipper.is_due(3, NOW - timedelta(minutes=5), NOW) is False
    assert shipper.is_due(3, NOW - timedelta(minutes=9), NOW) is True


def test_an_archive_row_is_sealed_carries_its_person_keys_and_opens_again():
    from nexus.engagement.ledger import envelope, shipper
    from nexus.engagement.ledger.pseudonym import person_key

    body = envelope.build(event_id="ev-1", event_type="message.sent", tenant_id="t-1",
                          occurred_at=NOW, payload={"body": "Hi Jane"})
    resolved = {"contact_email": "jane.buyer@acme.io", "person_emails": ["jane.buyer@acme.io"]}
    row = shipper.archive_row(SECRET, body, resolved)
    event_id, event_type, schema_version, occurred_at, tenant_id, sealed, keys = row
    assert (event_id, event_type, schema_version, tenant_id) == ("ev-1", "message.sent", 1, "t-1")
    assert occurred_at == NOW
    assert "Jane" not in sealed and "jane.buyer" not in sealed
    assert keys == [person_key(SECRET, "jane.buyer@acme.io")]
    opened = shipper.open_document(SECRET, sealed)
    assert opened["envelope"]["payload"]["body"] == "Hi Jane"
    assert opened["resolved"]["contact_email"] == "jane.buyer@acme.io"


async def test_shipping_without_a_secret_records_nothing_and_says_so(monkeypatch):
    from nexus.engagement.ledger import shipper
    from nexus.providers import resolver

    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", "")
    resolver.invalidate()
    result = await shipper.ship()
    assert result["shipped"] == 0 and "secret" in result["skipped"]


async def test_the_backlog_reports_what_is_waiting_and_how_old_it_is():
    from nexus.engagement.ledger import consent
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.ledger.shipper import backlog

    tid = await make_tenant(slug="ledgerq", name="Ledger Q")
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        await emit(ts, "account.scored", refs={"account_id": "a-1"}, payload={"composite": 80})
    waiting = await backlog()
    assert waiting["waiting"] >= 1 and waiting["retrying"] == 0


async def test_retention_only_removes_rows_the_archive_has_acknowledged():
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement.ledger import consent, shipper
    from nexus.engagement.ledger.emit import emit
    from nexus.models.ledger import LedgerOutbox

    tid = await make_tenant(slug="ledgerret", name="Ledger Ret")
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        await emit(ts, "account.scored", refs={"account_id": "a-1"}, payload={})
        await emit(ts, "account.scored", refs={"account_id": "a-2"}, payload={})
    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(LedgerOutbox).where(LedgerOutbox.tenant_id == tid))).scalars().all()
        rows[0].shipped_archive_at = utcnow() - timedelta(days=shipper.RETENTION_DAYS + 1)
        await session.commit()

    assert await shipper.purge_shipped() == 1
    async with get_platform_sessionmaker()() as session:
        left = (await session.execute(
            select(LedgerOutbox).where(LedgerOutbox.tenant_id == tid))).scalars().all()
    assert len(left) == 1 and left[0].shipped_archive_at is None


# ---- stores and schema ----------------------------------------------------------------------------

def test_every_store_has_versioned_sql_that_creates_its_schema():
    from nexus.engagement.ledger import schema, stores

    for store in stores.STORES:
        versions = schema.migrations(store)
        assert versions, f"{store} has no SQL"
        names = [version for version, _sql in versions]
        assert names == sorted(names)
        first = versions[0][1]
        assert "nexus_ledger.schema_migrations" in first
        assert "IF NOT EXISTS" in first, "applying a version twice must be safe"


async def test_an_unconfigured_store_is_reported_not_crashed(monkeypatch):
    from nexus.engagement.ledger import stores
    from nexus.providers import resolver

    for field in ("ledger_archive_dsn", "ledger_training_dsn", "ledger_insights_dsn"):
        monkeypatch.setattr(get_settings(), field, "")
    resolver.invalidate()
    report = await stores.status("archive")
    assert report["configured"] is False and report["detail"] == "not configured"
    assert report["pending"] == ["0001_initial"]
    with pytest.raises(ValueError):
        async with stores.connect("nope"):
            pass


async def test_a_store_dsn_pointing_at_the_metadata_service_is_refused(monkeypatch):
    """The SSRF guard the source-database subsystem owns, applied to operator-typed DSNs."""
    from nexus.engagement.ledger import stores
    from nexus.providers import resolver
    from nexus.sources.safety import SourceRejected

    monkeypatch.setattr(get_settings(), "env", "prod")
    monkeypatch.setattr(get_settings(), "source_db_allow_private", False)
    monkeypatch.setattr(get_settings(), "ledger_archive_dsn",
                        "postgresql://u:p@169.254.169.254:5432/postgres")
    resolver.invalidate()
    with pytest.raises(SourceRejected):
        async with stores.connect("archive"):
            pass


# ---- the jobs -------------------------------------------------------------------------------------

async def test_the_four_ledger_jobs_are_registered_and_do_nothing_without_a_store(monkeypatch):
    from nexus.providers import resolver
    from nexus.workers.tasks import HANDLERS

    for name in ("ship_ledger", "build_ledger_datasets", "ledger_delete_workspace",
                 "ledger_erase_person"):
        assert name in HANDLERS
    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", "")
    resolver.invalidate()
    assert "skipped" in await HANDLERS["ship_ledger"]({})
    assert await HANDLERS["ledger_delete_workspace"]({}) == {"error": "no tenant"}
    assert await HANDLERS["ledger_erase_person"]({}) == {"error": "no person key"}


async def test_the_scheduler_drives_shipping_and_building_whatever_automation_says(monkeypatch):
    from nexus.workers.queue import InMemoryTaskQueue
    from nexus.workers.scheduler import _enqueue_due

    monkeypatch.setattr(get_settings(), "automation_enabled", False)
    queue = InMemoryTaskQueue()
    await _enqueue_due(queue)
    names = set()
    while True:
        job = await queue.dequeue(timeout=0)
        if job is None:
            break
        names.add(job.name)
    assert {"ship_ledger", "build_ledger_datasets"} <= names


async def test_switching_training_off_queues_the_deletion_of_what_was_already_shipped(client):
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        token = await signup(client, slug="ledgeroff", email="owner@ledgeroff.com",
                             company="Ledger Off")
        r = await client.put("/api/engagement/settings/training", json={"status": "off"},
                             headers=auth(token))
        assert r.status_code == 200, r.text
        jobs = []
        while True:
            job = await queue.dequeue(timeout=0)
            if job is None:
                break
            jobs.append(job)
        assert any(j.name == "ledger_delete_workspace" for j in jobs)
    finally:
        set_task_queue(None)


async def test_erasing_a_shared_person_reaches_the_ledger(monkeypatch):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.people.store import forget_person, resolve_person_record
    from nexus.providers import resolver
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", SECRET)
    resolver.invalidate()
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        async with get_platform_sessionmaker()() as session:
            person = await resolve_person_record(session, email="jane.buyer@acme.io",
                                                 full_name="Jane Buyer")
            await session.commit()
            assert await forget_person(session, person.id) is True
            await session.commit()
        job = await queue.dequeue(timeout=0)
        assert job is not None and job.name == "ledger_erase_person"
        # The address itself is never in the payload: a dead-lettered job logs what it carries.
        assert "jane" not in json.dumps(job.payload).lower()
    finally:
        set_task_queue(None)


# ---- the admin surface ----------------------------------------------------------------------------

async def test_the_ledger_tab_is_platform_only_and_never_shows_a_connection_string(
    client, monkeypatch
):
    member = await signup(client, slug="ledgerui", email="rep@ledgerui.com", company="Ledger UI")
    # 404, not 403: the staff surface must not be enumerable (see `assert_staff_surface_hidden`).
    assert_staff_surface_hidden(await client.get("/api/admin/ledger", headers=auth(member)))

    token = await _superadmin(client, monkeypatch, slug="ledgeradm",
                              email="boss@ledgeradm.com")
    monkeypatch.setattr(get_settings(), "ledger_archive_dsn",
                        "postgresql://nexus_ledger:hunter2@db.example.com:5432/postgres")
    from nexus.providers import resolver

    resolver.invalidate()
    r = await client.get("/api/admin/ledger", headers=auth(token))
    assert r.status_code == 200, r.text
    assert "hunter2" not in r.text and "db.example.com" not in r.text
    body = r.json()
    assert [s["store"] for s in body["stores"]] == ["archive", "training", "insights"]
    assert body["outbox"]["waiting"] >= 0
    assert body["consented_workspaces"] >= 0


async def test_applying_a_schema_to_an_unconfigured_store_is_a_409(client, monkeypatch):
    from nexus.providers import resolver

    token = await _superadmin(client, monkeypatch, slug="ledgersch",
                              email="boss@ledgersch.com")
    monkeypatch.setattr(get_settings(), "ledger_training_dsn", "")
    resolver.invalidate()
    r = await client.post("/api/admin/ledger/training/schema", headers=auth(token))
    assert r.status_code == 409, r.text
    assert (await client.post("/api/admin/ledger/nope/schema",
                              headers=auth(token))).status_code == 404


async def test_erasing_a_person_queues_the_key_and_needs_the_secret(client, monkeypatch):
    from nexus.providers import resolver
    from nexus.workers.queue import InMemoryTaskQueue, set_task_queue

    token = await _superadmin(client, monkeypatch, slug="ledgererase",
                              email="boss@ledgererase.com")
    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", "")
    resolver.invalidate()
    r = await client.post("/api/admin/ledger/erase-person",
                          json={"email": "jane.buyer@acme.io"}, headers=auth(token))
    assert r.status_code == 409, r.text

    monkeypatch.setattr(get_settings(), "ledger_pseudonym_secret", SECRET)
    resolver.invalidate()
    queue = InMemoryTaskQueue()
    set_task_queue(queue)
    try:
        r = await client.post("/api/admin/ledger/erase-person",
                              json={"email": "jane.buyer@acme.io"}, headers=auth(token))
        assert r.status_code == 200, r.text
        from nexus.engagement.ledger.pseudonym import person_key

        assert r.json()["person_key"] == person_key(SECRET, "jane.buyer@acme.io")
        job = await queue.dequeue(timeout=0)
        assert job.name == "ledger_erase_person"
    finally:
        set_task_queue(None)


# ---- structure ------------------------------------------------------------------------------------

def test_the_ledger_tab_shows_state_and_the_two_actions_and_no_dsn():
    tab = (SRC / "pages/admin/LedgerTab.tsx").read_text(encoding="utf-8")
    assert "api.ledgerStatus" in tab
    assert "api.applyLedgerSchema" in tab and "api.eraseLedgerPerson" in tab
    for word in ("Waiting to ship", "contributing", "Datasets last built"):
        assert word in tab
    assert "dsn" not in tab.lower()
    host = (SRC / "pages/AdminBillingPage.tsx").read_text(encoding="utf-8")
    assert '{ value: "ledger", label: "Ledger" }' in host
    assert "<LedgerTab />" in host


def test_the_export_script_reads_only_the_training_store():
    script = (SCRIPTS / "export_training_dataset.py").read_text(encoding="utf-8")
    assert 'connect("training")' in script
    assert 'connect("archive")' not in script and 'connect("insights")' not in script
    assert "consent_terms_version" in script and "scrubber_version" in script


def test_the_setup_guide_names_the_role_the_schema_and_the_four_keys():
    guide = (pathlib.Path(__file__).resolve().parents[1]
             / "docs/engagement/setup-supabase.md").read_text(encoding="utf-8")
    for needle in ("create role nexus_ledger", "authorization nexus_ledger", "ledger_archive",
                   "ledger_training", "ledger_insights", "ledger_pseudonym"):
        assert needle in guide
