"""Which rows the workspace already holds, and which fields it already paid for.

Answered in a fixed number of queries per page whatever its size, through the TenantSession only.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import event

from nexus.core.db import get_engine
from tests.conftest import make_tenant, tenant_session
from tests.prospector_fixtures import COMPANIES, CONTACTS, many_contacts


@contextmanager
def _count_statements():
    seen: list[str] = []

    def before(conn, cursor, statement, *_):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    sync = get_engine().sync_engine
    event.listen(sync, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(sync, "before_cursor_execute", before)


async def _book(tid: str) -> dict:
    from nexus.models.account import Account, Contact

    async with tenant_session(tid) as ts:
        acme = Account(name="Acme Example Inc", domain="acme-example.com")
        initech = Account(name="Initech Example", domain="initech-example.co.uk")
        hooli = Account(name="Hooli, Inc.", domain=None)
        for a in (acme, initech, hooli):
            ts.add(a)
        await ts.flush()
        john = Contact(account_id=acme.id, full_name="John Smith", email="JOHN.SMITH@acme-example.com",
                       phone="+1 415 555 7890")
        wei = Contact(account_id=initech.id, full_name="Wei Lindqvist", email=None)
        gone = Contact(account_id=acme.id, full_name="Ana Okafor",
                       email="ana.okafor@umbrella-example.net")
        for c in (john, wei, gone):
            ts.add(c)
        await ts.flush()
        from nexus.core.db import utcnow

        gone.deleted_at = utcnow()
        await ts.flush()
        return {"acme": acme.id, "initech": initech.id, "hooli": hooli.id, "john": john.id,
                "wei": wei.id}


async def test_contacts_are_held_by_email_whatever_its_case_and_by_name_at_the_domain():
    from nexus.prospector.holdings import held_contacts
    from nexus.prospector.identity import contact_identity
    from nexus.prospector.records import parse_contact

    tid = await make_tenant("hold1")
    book = await _book(tid)
    records = [parse_contact(c) for c in CONTACTS]
    async with tenant_session(tid) as ts:
        held = await held_contacts(ts, records)

    by_ident = {contact_identity(r): r for r in records}
    john = held[contact_identity(records[0])]
    assert john.record_id == book["john"] and john.email == "JOHN.SMITH@acme-example.com"
    wei = held[contact_identity(records[2])]
    assert wei.record_id == book["wei"]
    # Deleted contacts are not held; strangers are not held.
    assert contact_identity(records[3]) not in held
    assert contact_identity(records[1]) not in held
    assert set(held) <= set(by_ident)


async def test_another_workspaces_contacts_are_never_seen():
    from nexus.prospector.holdings import held_contacts
    from nexus.prospector.records import parse_contact

    owner = await make_tenant("hold2a")
    await _book(owner)
    stranger = await make_tenant("hold2b")
    async with tenant_session(stranger) as ts:
        assert await held_contacts(ts, [parse_contact(c) for c in CONTACTS]) == {}


async def test_companies_are_held_by_domain_else_by_normalised_name():
    from nexus.prospector.holdings import held_companies
    from nexus.prospector.identity import company_identity
    from nexus.prospector.records import parse_company

    tid = await make_tenant("hold3")
    book = await _book(tid)
    records = [parse_company(c) for c in COMPANIES]
    records.append(parse_company({"CompanyName": "Hooli", "CompanyURL": ""}))
    async with tenant_session(tid) as ts:
        held = await held_companies(ts, records)

    assert held[company_identity(records[0])].record_id == book["acme"]
    assert held[company_identity(records[2])].record_id == book["initech"]
    assert held["n:hooli"].record_id == book["hooli"]
    assert company_identity(records[1]) not in held


async def test_a_page_is_answered_in_a_fixed_number_of_queries():
    from nexus.prospector.holdings import held_contacts
    from nexus.prospector.records import parse_contact

    tid = await make_tenant("hold4")
    await _book(tid)
    small = [parse_contact(c) for c in many_contacts(5)]
    large = [parse_contact(c) for c in many_contacts(200)]
    async with tenant_session(tid) as ts:
        with _count_statements() as few:
            await held_contacts(ts, small)
        with _count_statements() as many:
            await held_contacts(ts, large)
    assert len(few) == len(many) <= 2


async def test_purchases_are_read_per_field():
    from nexus.core.db import utcnow
    from nexus.models.prospector import ProspectorPurchase
    from nexus.prospector.holdings import purchased

    tid = await make_tenant("hold5")
    async with tenant_session(tid) as ts:
        now = utcnow()
        ts.add(ProspectorPurchase(tenant_id=tid, fingerprint="f" * 64, field="email",
                                  request_id="r", first_charged_at=now, last_delivered_at=now))
        await ts.flush()
        assert await purchased(ts, ["f" * 64, "g" * 64]) == {("f" * 64, "email")}
        assert await purchased(ts, []) == set()
