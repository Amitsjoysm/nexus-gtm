"""Net-new contact search: stub determinism + registry waterfall/dedupe/policy. Offline."""
from __future__ import annotations

from nexus.integrations.contact_search import (
    ContactCandidate,
    ContactSearchProvider,
    SearchBackedContactSearchProvider,
    StubContactSearchProvider,
    _parse_people,
)
from nexus.agents.llm import StubLLMProvider
from nexus.integrations.registry import DataSourceRegistry, _build_contact_search
from nexus.models.account import Account
from tests.conftest import make_tenant, tenant_session


async def test_stub_returns_one_persona_per_buyer_title_up_to_limit():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        prov = StubContactSearchProvider()
        cands = await prov.search(acc, {"buyer_titles": ["VP Sales", "CRO"]}, limit=3)
    # One persona per buyer title (a buying committee), not a single generic "Lead".
    assert [c.title for c in cands] == ["VP Sales", "CRO"]
    assert cands[0].full_name == "Acme VP Sales"
    assert all(c.email is None and c.source == "stub" for c in cands)


async def test_stub_caps_personas_at_limit():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        cands = await StubContactSearchProvider().search(
            acc, {"buyer_titles": ["A", "B", "C", "D"]}, limit=2)
    assert [c.title for c in cands] == ["A", "B"]


async def test_stub_defaults_to_buying_committee_when_no_buyer_titles():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        cands = await StubContactSearchProvider().search(acc, {}, limit=3)
    # Falls back to a default committee so contacts discovery isn't empty.
    assert len(cands) == 3
    assert cands[0].title == "VP Sales"


class _Hit:
    def __init__(self, title, snippet="", url=""):
        self.title, self.snippet, self.url = title, snippet, url


class _FakeSearch:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, q, *, limit=5):
        return self._hits


class _FakeLLM:
    def __init__(self, text):
        self.text = text

    async def complete(self, messages, **k):
        from nexus.agents.llm import LLMResponse

        return LLMResponse(text=self.text)


def test_parse_people_tolerates_prose_and_garbage():
    assert _parse_people('Here you go: [{"full_name":"A"}] done') == [{"full_name": "A"}]
    assert _parse_people("no array here") == []
    assert _parse_people("[stub] not json") == []
    assert _parse_people("{}") == []
    assert _parse_people(None) == []


async def test_search_contact_extracts_real_names_via_llm():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com")
    hits = [_Hit("Jane Smith - VP Sales at Acme | LinkedIn", "Jane Smith, VP Sales, Acme")]
    llm = _FakeLLM('[{"full_name":"Jane Smith","title":"VP Sales","seniority":"VP",'
                   '"linkedin_url":"https://linkedin.com/in/jane"}]')
    prov = SearchBackedContactSearchProvider(_FakeSearch(hits), llm)
    out = await prov.search(acc, {"titles": ["VP Sales"]}, limit=5)
    assert len(out) == 1
    assert out[0].full_name == "Jane Smith" and out[0].title == "VP Sales"
    assert out[0].source == "search" and out[0].linkedin_url.endswith("jane")


async def test_search_contact_empty_when_llm_is_stub():
    """Offline (stub LLM) extracts no names -> [], so the caller falls back to the role stub."""
    acc = Account(tenant_id="t", name="Acme", domain="acme.com")
    prov = SearchBackedContactSearchProvider(_FakeSearch([_Hit("x", "y")]), StubLLMProvider())
    assert await prov.search(acc, {"titles": ["VP Sales"]}, limit=5) == []


async def test_search_contact_empty_when_no_hits():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com")
    prov = SearchBackedContactSearchProvider(_FakeSearch([]), _FakeLLM("[]"))
    assert await prov.search(acc, {"titles": ["VP Sales"]}, limit=5) == []


def test_build_contact_search_wires_search_token_when_provider_present():
    provs = _build_contact_search(["search", "stub"], search=_FakeSearch([]))
    assert [p.name for p in provs] == ["search", "stub"]
    # Without a search provider, the 'search' token is skipped (degrades to stub).
    provs2 = _build_contact_search(["search"], search=None)
    assert [p.name for p in provs2] == ["stub"]


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
