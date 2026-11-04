"""Bulk delete from the Accounts and Contacts lists (SDR round 2, 5.5; owner decision 2026-10-29).

* Contacts: up to 500 at once, soft-deleted exactly as one is, restorable.
* Accounts: deleted for good with their contacts, refused while anyone there is in a running
  campaign. The shared company record is linked first and stays, for other workspaces and this
  one's later campaigns. "Don't suggest these again here" suppresses the domains for this
  workspace's discovery; "Report wrong company data" flags the shared row and never quarantines it.

SQLite checks no foreign key, so a reference the purge forgot would pass silently here and fail on
Postgres. `test_nothing_is_left_pointing_at_what_was_deleted` therefore builds a row in every table
that points at an account or contact and scans every foreign key afterwards.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import insert, select

from nexus.core.security import create_access_token
from tests.conftest import auth, make_tenant, tenant_session


def _h(tid: str, role: str = "manager", user: str = "u-mgr") -> dict:
    return auth(create_access_token(user_id=user, tenant_id=tid, role=role))


def _filler(column):
    """A value for a NOT NULL column the test does not care about."""
    from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, Numeric
    from sqlalchemy.types import TypeDecorator

    from nexus.core.db import utcnow

    kind = column.type.impl if isinstance(column.type, TypeDecorator) else column.type
    if isinstance(kind, Boolean):
        return False
    if isinstance(kind, (Integer, Float, Numeric)):
        return 0
    if isinstance(kind, JSON):
        return {}
    if isinstance(kind, DateTime):
        return utcnow()
    if isinstance(kind, Date):
        return date.today()
    # Unique, so two rows never collide on a dedupe key the test does not care about.
    from nexus.core.db import new_id

    return new_id()[: getattr(kind, "length", None) or 32]


async def _row(ts, table_name: str, **values) -> str:
    """Insert one row with only what matters to the test; everything else required is filler."""
    from nexus.core.db import Base, new_id

    table = Base.metadata.tables[table_name]
    row = {"id": new_id(), "tenant_id": ts.tenant_id, **values}
    for column in table.columns:
        if column.name in row or column.nullable or column.primary_key:
            continue
        if column.default is None and column.server_default is None:
            row[column.name] = _filler(column)
    await ts.session.execute(insert(table).values(**row))
    return row["id"]


async def _account(ts, name: str, domain: str | None, **extra) -> str:
    return await _row(ts, "accounts", name=name, domain=domain, tech_stack=[], custom_fields={},
                      **extra)


async def _contact(ts, account_id: str, email: str) -> str:
    return await _row(ts, "contacts", account_id=account_id, full_name=email.split("@")[0],
                      email=email, custom_fields={})


async def _graph(ts, account_id: str, contact_id: str) -> dict[str, str]:
    """A row in every table that points at an account or a contact, for this pair."""
    ids: dict[str, str] = {}
    ids["score"] = await _row(ts, "account_scores", account_id=account_id)
    ids["snapshot"] = await _row(ts, "page_snapshots", account_id=account_id)
    ids["signal"] = await _row(ts, "signal_events", account_id=account_id, contact_id=contact_id)
    ids["alert"] = await _row(ts, "alerts", account_id=account_id, signal_id=ids["signal"])
    ids["task"] = await _row(ts, "inbox_tasks", account_id=account_id, contact_id=contact_id,
                             signal_id=ids["signal"])
    ids["list"] = await _row(ts, "prospect_lists", name="Targets")
    ids["list_item"] = await _row(ts, "list_items", list_id=ids["list"], account_id=account_id,
                                  contact_id=contact_id)
    ids["campaign"] = await _row(ts, "campaigns", list_id=ids["list"])
    ids["target"] = await _row(ts, "campaign_targets", campaign_id=ids["campaign"],
                               account_id=account_id)
    ids["cadence"] = await _row(ts, "cadences", name="Old")
    ids["cadence_enrollment"] = await _row(
        ts, "cadence_enrollments", cadence_id=ids["cadence"], account_id=account_id,
        contact_id=contact_id, campaign_target_id=ids["target"])
    ids["touch"] = await _row(ts, "cadence_touches", enrollment_id=ids["cadence_enrollment"])
    ids["mailbox"] = await _row(ts, "mailbox_connections", owner_user_id="u-mgr",
                                email=f"sdr-{account_id[:6]}@ours.io")
    ids["eng_campaign"] = await _row(ts, "engagement_campaigns", owner_user_id="u-mgr",
                                     name="Done", status="completed")
    ids["enrollment"] = await _row(ts, "engagement_enrollments", campaign_id=ids["eng_campaign"],
                                   contact_id=contact_id, account_id=account_id,
                                   status="completed")
    ids["thread"] = await _row(ts, "engagement_threads", mailbox_connection_id=ids["mailbox"],
                               provider_thread_id=f"t-{account_id}", contact_id=contact_id,
                               account_id=account_id, enrollment_id=ids["enrollment"])
    ids["message"] = await _row(ts, "engagement_messages", mailbox_connection_id=ids["mailbox"],
                                thread_id=ids["thread"], enrollment_id=ids["enrollment"],
                                contact_id=contact_id, direction="out", status="sent",
                                step_index=0)
    ids["reply"] = await _row(ts, "reply_classifications", message_id=ids["message"],
                              mailbox_connection_id=ids["mailbox"],
                              enrollment_id=ids["enrollment"], contact_id=contact_id,
                              account_id=account_id, category="declined")
    ids["call_task"] = await _row(ts, "call_tasks", account_id=account_id, contact_id=contact_id,
                                  engagement_enrollment_id=ids["enrollment"],
                                  cadence_enrollment_id=ids["cadence_enrollment"])
    ids["call"] = await _row(ts, "call_activities", call_task_id=ids["call_task"],
                             account_id=account_id, contact_id=contact_id)
    ids["dnc"] = await _row(ts, "do_not_contact", email=f"blocked-{contact_id[:6]}@x.io",
                            contact_id=contact_id, reason="declined")
    ids["run"] = await _row(ts, "orchestration_runs", account_id=account_id, goal="research")
    ids["agent_run"] = await _row(ts, "agent_runs", account_id=account_id)
    ids["chat"] = await _row(ts, "chat_sessions", account_id=account_id)
    ids["play"] = await _row(ts, "plays", name="Funding")
    ids["play_run"] = await _row(ts, "play_runs", play_id=ids["play"], account_id=account_id,
                                 signal_id=ids["signal"])
    ids["outcome"] = await _row(ts, "outcomes", account_id=account_id, contact_id=contact_id)
    ids["source_run"] = await _row(ts, "signal_source_runs", account_id=account_id)
    # Its fit against a further service (W8, 0077): goes with the account; the service stays.
    ids["service"] = await _row(ts, "icp_services", name=f"Service {account_id[:6]}")
    ids["service_fit"] = await _row(ts, "account_service_fits", account_id=account_id,
                                    service_id=ids["service"], reasons=[])
    return ids


def test_every_table_pointing_at_an_account_or_contact_is_in_the_graph():
    """A new table that points at an account must be decided (owned, or kept with the pointer
    cleared) and exercised here, or the purge could strand it on Postgres."""
    import nexus.models  # noqa: F401
    from nexus.core.db import Base

    pointing = {
        t.name for t in Base.metadata.sorted_tables for fk in t.foreign_keys
        if fk.column.table.name in ("accounts", "contacts")
    }
    covered = {
        "account_scores", "page_snapshots", "signal_events", "alerts", "inbox_tasks", "list_items",
        "campaign_targets", "cadence_enrollments", "engagement_enrollments", "engagement_threads",
        "engagement_messages", "reply_classifications", "call_tasks", "call_activities",
        "do_not_contact", "orchestration_runs", "agent_runs", "chat_sessions", "play_runs",
        "outcomes", "signal_source_runs", "contacts", "account_service_fits",
    }
    assert pointing == covered, (
        f"new: {sorted(pointing - covered)}; gone: {sorted(covered - pointing)}")


async def _seeded(tid: str):
    async with tenant_session(tid) as ts:
        gone = await _account(ts, "Globex", "globex.io", industry="Logistics",
                              employee_count=1200, country="United States")
        gone_contact = await _contact(ts, gone, "sam@globex.io")
        graph = await _graph(ts, gone, gone_contact)
        kept = await _account(ts, "Initech", "initech.io")
        kept_contact = await _contact(ts, kept, "ann@initech.io")
        kept_graph = await _graph(ts, kept, kept_contact)
        # A row on the account that stays, pointing at a signal of the one that goes.
        cross_alert = await _row(ts, "alerts", account_id=kept, signal_id=graph["signal"])
    return {"gone": gone, "gone_contact": gone_contact, "graph": graph, "kept": kept,
            "kept_contact": kept_contact, "kept_graph": kept_graph, "cross_alert": cross_alert}


async def test_nothing_is_left_pointing_at_what_was_deleted(client):
    import nexus.models  # noqa: F401
    from nexus.core.db import Base

    tid = await make_tenant("bd1", "BD1")
    s = await _seeded(tid)

    r = await client.post("/api/accounts/bulk-delete", headers=_h(tid), json={"ids": [s["gone"]]})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1 and r.json()["contacts_deleted"] == 1

    tables = Base.metadata.tables
    async with tenant_session(tid) as ts:
        async def exists(table: str, row_id: str) -> bool:
            t = tables[table]
            return (await ts.session.execute(select(t.c.id).where(t.c.id == row_id))).first() \
                is not None

        g = s["graph"]
        for owned in ("score", "snapshot", "signal", "alert", "task", "list_item", "target",
                      "cadence_enrollment", "touch", "enrollment", "call_task", "call",
                      "service_fit"):
            table = {"score": "account_scores", "snapshot": "page_snapshots",
                     "service_fit": "account_service_fits",
                     "signal": "signal_events", "alert": "alerts", "task": "inbox_tasks",
                     "list_item": "list_items", "target": "campaign_targets",
                     "cadence_enrollment": "cadence_enrollments", "touch": "cadence_touches",
                     "enrollment": "engagement_enrollments", "call_task": "call_tasks",
                     "call": "call_activities"}[owned]
            assert not await exists(table, g[owned]), f"{table} row should be gone"
        assert not await exists("accounts", s["gone"])
        assert not await exists("contacts", s["gone_contact"])

        # Somebody else's records stay, without the pointer.
        for kept, table in (("run", "orchestration_runs"), ("agent_run", "agent_runs"),
                            ("chat", "chat_sessions"), ("play_run", "play_runs"),
                            ("outcome", "outcomes"), ("dnc", "do_not_contact"),
                            ("thread", "engagement_threads"), ("message", "engagement_messages"),
                            ("reply", "reply_classifications"), ("source_run", "signal_source_runs"),
                            ("list", "prospect_lists"), ("mailbox", "mailbox_connections"),
                            ("service", "icp_services")):
            assert await exists(table, g[kept]), f"{table} row should be kept"
        dnc = tables["do_not_contact"]
        email = (await ts.session.execute(select(dnc.c.email, dnc.c.contact_id)
                                          .where(dnc.c.id == g["dnc"]))).first()
        assert email.email.startswith("blocked-") and email.contact_id is None

        # The other account and everything of its own are untouched; its alert lost the pointer.
        assert await exists("accounts", s["kept"]) and await exists("contacts", s["kept_contact"])
        for key, table in (("signal", "signal_events"), ("enrollment", "engagement_enrollments"),
                           ("call", "call_activities")):
            assert await exists(table, s["kept_graph"][key])
        alerts = tables["alerts"]
        cross = (await ts.session.execute(select(alerts.c.signal_id)
                                          .where(alerts.c.id == s["cross_alert"]))).first()
        assert cross is not None and cross.signal_id is None

        # The scan: no foreign key anywhere points at a deleted row.
        deleted = {"accounts": {s["gone"]}, "contacts": {s["gone_contact"]},
                   "signal_events": {g["signal"]}, "engagement_enrollments": {g["enrollment"]},
                   "cadence_enrollments": {g["cadence_enrollment"]}, "call_tasks": {g["call_task"]},
                   "campaign_targets": {g["target"]}}
        for table in Base.metadata.sorted_tables:
            for fk in table.foreign_keys:
                gone_ids = deleted.get(fk.column.table.name)
                if not gone_ids:
                    continue
                left = (await ts.session.execute(
                    select(fk.parent).where(fk.parent.in_(gone_ids)))).all()
                assert not left, f"{table.name}.{fk.parent.name} still points at a deleted row"


async def test_the_shared_company_record_stays_linked_and_knows_what_the_account_knew(client):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company

    tid = await make_tenant("bd2", "BD2")
    async with tenant_session(tid) as ts:
        gone = await _account(ts, "Globex", "www.Globex.io", industry="Logistics",
                              employee_count=1200, country="United States")
    r = await client.post("/api/accounts/bulk-delete", headers=_h(tid), json={"ids": [gone]})
    assert r.status_code == 200, r.text
    assert r.json()["shared_records_kept"] == 1

    async with get_platform_sessionmaker()() as s:
        company = (await s.scalars(select(Company).where(Company.domain == "globex.io"))).first()
    assert company is not None, "the master record outlives the account"
    assert company.industry == "Logistics" and company.employee_count == 1200
    assert company.labels_quarantined_at is None


async def test_dont_suggest_these_again_stops_discovery_offering_them(client):
    from nexus.models.company import Company
    from nexus.prospecting.companies import ProspectQuery, _held, _matching

    tid = await make_tenant("bd3", "BD3")
    async with tenant_session(tid) as ts:
        gone = await _account(ts, "Globex", "globex.io")
    r = await client.post("/api/accounts/bulk-delete", headers=_h(tid),
                          json={"ids": [gone], "suppress": True})
    assert r.status_code == 200 and r.json()["suppressed"] == 1

    from nexus.core.db import get_platform_sessionmaker

    async with get_platform_sessionmaker()() as s:
        company = (await s.scalars(select(Company).where(Company.domain == "globex.io"))).one()
        company.linkedin_industry_id = 4
        await s.commit()

    async with tenant_session(tid) as ts:
        assert await _held(ts, ["globex.io", "hooli.io"]) == {"globex.io"}
        offered = (await ts.session.scalars(
            _matching(ts, ProspectQuery(industry_ids=(4,)), set()))).all()
        assert "globex.io" not in {c.domain for c in offered}

    # Per workspace: another one is still offered the company.
    other = await make_tenant("bd3b", "BD3B")
    async with tenant_session(other) as ts:
        assert await _held(ts, ["globex.io"]) == set()
        offered = (await ts.session.scalars(
            _matching(ts, ProspectQuery(industry_ids=(4,)), set()))).all()
        assert "globex.io" in {c.domain for c in offered}


async def test_report_wrong_company_data_flags_the_row_and_never_quarantines_it(client):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company
    from nexus.models.records import CompanyReport

    tid = await make_tenant("bd4", "BD4")
    async with tenant_session(tid) as ts:
        gone = await _account(ts, "Globex", "globex.io")

    refused = await client.post("/api/accounts/bulk-delete", headers=_h(tid),
                                json={"ids": [gone], "report_wrong": True})
    assert refused.status_code == 422, "a report needs a reason a reviewer can check"

    r = await client.post("/api/accounts/bulk-delete", headers=_h(tid), json={
        "ids": [gone], "report_wrong": True, "reason": "This is Globex Corp, not Globex Ltd"})
    assert r.status_code == 200 and r.json()["reported"] == 1

    async with get_platform_sessionmaker()() as s:
        company = (await s.scalars(select(Company).where(Company.domain == "globex.io"))).one()
        report = (await s.scalars(select(CompanyReport))).one()
    assert report.company_id == company.id and report.subject_tenant_id == tid
    assert report.status == "open" and "Globex Corp" in report.reason
    assert company.labels_quarantined_at is None and company.name


async def test_an_account_in_a_running_campaign_is_refused_and_named(client):
    from nexus.models.account import Account

    tid = await make_tenant("bd5", "BD5")
    async with tenant_session(tid) as ts:
        busy = await _account(ts, "Globex", "globex.io")
        free = await _account(ts, "Initech", "initech.io")
        contact = await _contact(ts, busy, "sam@globex.io")
        campaign = await _row(ts, "engagement_campaigns", owner_user_id="u-mgr",
                              name="Q4 Logistics", status="active")
        await _row(ts, "engagement_enrollments", campaign_id=campaign, contact_id=contact,
                   account_id=busy, status="active")

    preview = await client.post("/api/accounts/bulk-delete/preview", headers=_h(tid),
                                json={"ids": [busy, free]})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["accounts"] == 2 and body["contacts"] == 1
    assert body["in_running_campaigns"] == [
        {"id": busy, "name": "Globex", "campaigns": ["Q4 Logistics"]}]

    r = await client.post("/api/accounts/bulk-delete", headers=_h(tid), json={"ids": [busy, free]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Globex" in detail["message"] and "Q4 Logistics" in detail["message"]
    async with tenant_session(tid) as ts:
        assert len(await ts.list(Account)) == 2, "nothing is deleted when one is refused"

    ok = await client.post("/api/accounts/bulk-delete", headers=_h(tid), json={"ids": [free]})
    assert ok.status_code == 200 and ok.json()["deleted"] == 1


async def test_a_rep_cannot_bulk_delete_and_a_workspace_cannot_delete_another_s(client):
    from nexus.models.account import Account, Contact

    tid = await make_tenant("bd6", "BD6")
    other = await make_tenant("bd6b", "BD6B")
    async with tenant_session(tid) as ts:
        account = await _account(ts, "Globex", "globex.io")
        contact = await _contact(ts, account, "sam@globex.io")

    rep = await client.post("/api/accounts/bulk-delete", headers=_h(tid, "rep", "u-rep"),
                            json={"ids": [account]})
    assert rep.status_code == 403
    rep_c = await client.post("/api/contacts/bulk-delete", headers=_h(tid, "rep", "u-rep"),
                              json={"ids": [contact]})
    assert rep_c.status_code == 403

    stranger = await client.post("/api/accounts/bulk-delete", headers=_h(other),
                                 json={"ids": [account]})
    assert stranger.status_code == 200 and stranger.json()["deleted"] == 0
    stranger_c = await client.post("/api/contacts/bulk-delete", headers=_h(other),
                                   json={"ids": [contact]})
    assert stranger_c.json()["deleted"] == 0
    async with tenant_session(tid) as ts:
        assert await ts.get(Account, account) is not None
        assert (await ts.get(Contact, contact)).deleted_at is None


async def test_more_than_500_is_refused(client):
    tid = await make_tenant("bd7", "BD7")
    ids = [f"a{i}" for i in range(501)]
    for path in ("/api/accounts/bulk-delete", "/api/contacts/bulk-delete"):
        r = await client.post(path, headers=_h(tid), json={"ids": ids})
        assert r.status_code == 422, path


async def test_contacts_are_soft_deleted_in_bulk_and_can_be_restored(client):
    from nexus.models.account import Contact

    tid = await make_tenant("bd8", "BD8")
    async with tenant_session(tid) as ts:
        account = await _account(ts, "Globex", "globex.io")
        ids = [await _contact(ts, account, f"p{i}@globex.io") for i in range(3)]

    r = await client.post("/api/contacts/bulk-delete", headers=_h(tid), json={"ids": ids[:2]})
    assert r.status_code == 200 and r.json() == {"deleted": 2, "restorable": True}
    listed = await client.get("/api/contacts", headers=_h(tid))
    assert {c["id"] for c in listed.json()} == {ids[2]}
    async with tenant_session(tid) as ts:
        rows = await ts.list(Contact)
        assert len(rows) == 3, "the rows survive, as one contact's delete has always kept them"

    back = await client.post("/api/contacts/bulk-restore", headers=_h(tid), json={"ids": ids[:2]})
    assert back.json() == {"restored": 2}
    listed = await client.get("/api/contacts", headers=_h(tid))
    assert {c["id"] for c in listed.json()} == set(ids)


async def test_an_undecided_reference_refuses_the_whole_delete(monkeypatch):
    """A table pointing at an account with a NOT NULL column, missing from OWNED, must stop the
    delete before anything changes rather than strand its rows."""
    from nexus.accounts import purge
    from nexus.models.account import Account

    tid = await make_tenant("bd9", "BD9")
    async with tenant_session(tid) as ts:
        account = await _account(ts, "Globex", "globex.io")
        await _row(ts, "account_scores", account_id=account)

    monkeypatch.setattr(purge, "OWNED", tuple(o for o in purge.OWNED if o[0] != "account_scores"))
    with pytest.raises(purge.PurgeIncomplete):
        async with tenant_session(tid) as ts:
            await purge.purge_accounts(ts, [account])
    async with tenant_session(tid) as ts:
        assert await ts.get(Account, account) is not None
