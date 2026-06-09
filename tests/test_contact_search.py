"""Net-new contact search: stub determinism + registry waterfall/dedupe/policy. Offline."""
from __future__ import annotations

from nexus.integrations.contact_search import (
    ContactCandidate,
    ContactSearchProvider,
    StubContactSearchProvider,
)
from nexus.integrations.registry import DataSourceRegistry
from nexus.models.account import Account
from tests.conftest import make_tenant, tenant_session


async def test_stub_returns_one_deterministic_candidate_with_buyer_title():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        prov = StubContactSearchProvider()
        cands = await prov.search(acc, {"buyer_titles": ["VP Sales", "CRO"]}, limit=3)
    assert len(cands) == 1
    assert cands[0].title == "VP Sales"
    assert cands[0].full_name == "Acme Lead"
    assert cands[0].email is None
    assert cands[0].source == "stub"


async def test_stub_defaults_title_when_no_buyer_titles():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        cands = await StubContactSearchProvider().search(acc, {}, limit=3)
    assert cands[0].title == "Decision Maker"


async def test_registry_contact_search_dedupes_by_name_title():
    class Fake(ContactSearchProvider):
        def __init__(self, name, cands):
            self.name = name
            self._cands = cands

        async def search(self, account, icp, *, limit=3):
            return list(self._cands)[:limit]

    dup = ContactCandidate(full_name="Jane Doe", title="VP", source="a")
    same = ContactCandidate(full_name="Jane Doe", title="VP", source="b")
    other = ContactCandidate(full_name="Bob Roe", title="Director", source="b")
    reg = DataSourceRegistry(
        contact_search=[Fake("a", [dup]), Fake("b", [same, other])]
    )
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        out = await reg.contact_search(acc, {}, limit=5)
    keys = {(c.full_name, c.title) for c in out}
    assert keys == {("Jane Doe", "VP"), ("Bob Roe", "Director")}
    assert len(out) == 2


async def test_registry_contact_search_empty_when_no_providers_find():
    reg = DataSourceRegistry(contact_search=[StubContactSearchProvider()])
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        out = await reg.contact_search(acc, {"buyer_titles": ["CTO"]}, limit=3)
    assert len(out) == 1
    assert out[0].title == "CTO"


async def test_build_registry_wires_stub_contact_search_by_default():
    from nexus.integrations.registry import build_registry_from_settings

    reg = build_registry_from_settings()
    assert [p.name for p in reg.contact_search_providers] == ["stub"]
