# tests/test_mcp_tools.py
"""The tool catalogue (spec 2026-10-13, task 5): its shape is what the marketplaces review, and its
handlers run through the real API on an offline workspace."""
from __future__ import annotations

import re

import pytest

from nexus.mcp import credentials
from nexus.mcp.gateway import Gateway, ToolOutcome, path_allowed
from nexus.mcp.schema import SUPPORTED_KEYWORDS, SchemaError, validate
from nexus.mcp.tools import TOOLS, by_name, describe
from tests.conftest import auth, principal_from_token, signup

VENDORS = ("exa", "apify", "groq", "firecrawl", "serper", "brave", "reacher", "harvestapi",
           "anthropic", "openai", "duckduckgo", "twilio", "stripe")


# ---- shape -----------------------------------------------------------------------------------

def test_every_tool_has_a_title_and_the_hints_the_directories_require():
    for t in TOOLS:
        ann = t.annotations()
        assert t.title and ann["title"] == t.title, t.name
        assert "readOnlyHint" in ann, t.name
        if not ann["readOnlyHint"]:
            assert "destructiveHint" in ann and ann["destructiveHint"] is False, t.name


def test_names_are_unique_short_and_snake_case():
    names = [t.name for t in TOOLS]
    # 36 since 2026-10-19 (most of the product, owner decisions): under the ~40 tools some clients
    # cap across every connected server.
    assert len(names) == len(set(names)) == 36
    for n in names:
        assert len(n) <= 64 and re.fullmatch(r"[a-z][a-z_]*", n), n


def test_the_order_is_read_then_research_then_write():
    order = [t.scope for t in TOOLS]
    assert order == sorted(order, key=["read", "research", "write"].index)
    assert set(order) == {"read", "research", "write"}


def test_read_tools_are_the_free_ones():
    # prospector_search browses the prospect database masked, which is free (decision D3).
    assert [t.name for t in TOOLS if t.scope == "read"] == [
        "search", "fetch", "list_signals", "list_inbox", "list_lists", "list_campaigns",
        "get_credit_balance", "prospector_search", "get_workspace_overview", "list_queue",
        "search_network"]


def test_nothing_reaches_approve_launch_send_dial_or_delete():
    """The line the app keeps (spec 2026-10-13, kept by the owner on 2026-10-19)."""
    for t in TOOLS:
        for method, path in t.calls:
            assert method in ("GET", "POST"), (t.name, method)
            assert not re.search(r"/(approve|approve-all|launch|send|send-email|dial|archive|merge"
                                 r"|owner|delete)(/|$)", path), (t.name, path)


def test_descriptions_name_no_vendor():
    for t in TOOLS:
        text = f"{t.title} {t.description} {t.input_schema}".lower()
        for vendor in VENDORS:
            assert not re.search(rf"\b{vendor}\b", text), (t.name, vendor)


def _keywords(schema: dict):
    for key, value in schema.items():
        yield key
        if key == "properties":
            for rule in value.values():
                yield from _keywords(rule)
        elif key == "items" and isinstance(value, dict):
            yield from _keywords(value)


def test_schemas_use_only_what_the_validator_enforces():
    for t in TOOLS:
        assert t.input_schema["type"] == "object"
        assert t.input_schema.get("additionalProperties") is False, t.name
        unknown = set(_keywords(t.input_schema)) - SUPPORTED_KEYWORDS
        assert not unknown, (t.name, unknown)


def test_research_tools_declare_their_price_and_write_tools_do_not():
    for t in TOOLS:
        assert bool(t.price_capability) == (t.scope == "research"), t.name


def test_every_declared_call_is_reachable_through_the_gateway():
    for t in TOOLS:
        assert t.calls, t.name
        for method, template in t.calls:
            assert path_allowed(method, template.replace("{id}", "abc123")), (t.name, template)


async def test_handlers_only_send_the_requests_they_declare():
    class Recorder:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def get(self, path, params=None):
            self.sent.append(("GET", path))
            return ToolOutcome(True, {"account_id": "acc1", "id": "x1"} if "contacts/" in path
                               else [])

        async def post(self, path, json=None, params=None, idempotency_key=None):
            self.sent.append(("POST", path))
            return ToolOutcome(True, {"id": "x1", "output": {}})

    samples = {"id": "account:abc123", "query": "a", "account_id": "abc123", "contact_id": "abc123",
               "list_id": "abc123", "campaign_id": "abc123", "task_id": "abc123", "name": "n",
               "body": "b", "reply_id": "abc123", "alert_id": "abc123", "kind": "alerts",
               "count": 1, "disposition": "connected"}
    for t in TOOLS:
        rec = Recorder()
        props = t.input_schema["properties"]
        # A sample a tool's own enum does not allow (create_list's kind) is left to its default.
        args = validate(t.input_schema, {k: v for k, v in samples.items() if k in props
                                         and v in props[k].get("enum", [v])})
        await t.handler(rec, args)  # type: ignore[arg-type]
        declared = {(m, tpl.replace("{id}", "abc123")) for m, tpl in t.calls}
        declared |= {(m, tpl.replace("{id}", "acc1")) for m, tpl in t.calls}
        # An id the handler got back from its own earlier call (queue_call's new task).
        declared |= {(m, tpl.replace("{id}", "x1")) for m, tpl in t.calls}
        for sent in rec.sent:
            assert sent in declared, (t.name, sent)
            assert path_allowed(*sent)


def test_a_path_traversal_id_is_a_schema_error():
    with pytest.raises(SchemaError):
        validate(by_name("find_contacts").input_schema, {"account_id": "../admin"})
    with pytest.raises(SchemaError):
        validate(by_name("fetch").input_schema, {"id": "account:../x"})


def test_a_research_tool_states_its_price_from_the_rate_card():
    entry = describe(by_name("enrich_contact"), {"enrich.contact": 4})
    assert "4 credits per contact" in entry["description"]
    assert entry["annotations"]["openWorldHint"] is True
    fallback = describe(by_name("enrich_contact"), {})
    assert "credits" in fallback["description"] and "Costs credits" in fallback["description"]


# ---- behaviour on an offline workspace -----------------------------------------------------

def _actor(token: str) -> credentials.Actor:
    p = principal_from_token(token)
    return credentials.Actor(
        user_id=p.user_id, tenant_id=p.tenant_id, role=p.role, token_version=0,
        connection_id="conn-test", kind="api_key", scopes=frozenset(credentials.SCOPES),
        name="test")


async def _call(client, token, tool, **args):
    spec = by_name(tool)
    gw = Gateway(client._transport.app, _actor(token))
    return await spec.handler(gw, validate(spec.input_schema, args))


async def test_create_then_search_then_fetch_an_account(client):
    token = await signup(client, slug="tl1", email="o@tl1.com")
    made = await _call(client, token, "create_account", name="Globex", domain="globex.com")
    assert made.ok, made.message
    acc_id = made.data["account"]["id"]
    found = await _call(client, token, "search", query="globex")
    assert found.ok and found.data["results"][0]["id"] == f"account:{acc_id}"
    got = await _call(client, token, "fetch", id=f"account:{acc_id}")
    assert got.ok and got.data["account"]["name"] == "Globex"
    assert set(got.data) >= {"signals", "contacts"}


async def test_lists_can_be_made_filled_and_read(client):
    token = await signup(client, slug="tl2", email="o@tl2.com")
    acc = (await _call(client, token, "create_account", name="Initech")).data["account"]["id"]
    made = await _call(client, token, "create_list", name="Q4 targets")
    assert made.ok, made.message
    list_id = made.data["list"]["id"]
    added = await _call(client, token, "add_to_list", list_id=list_id, account_ids=[acc])
    assert added.ok and added.data["added"] == 1
    lists = await _call(client, token, "list_lists")
    assert lists.ok and any(row["id"] == list_id for row in lists.data["lists"])
    one = await _call(client, token, "fetch", id=f"list:{list_id}")
    assert one.ok and one.data["members"][0]["account_id"] == acc


async def test_the_read_tools_answer(client):
    token = await signup(client, slug="tl3", email="o@tl3.com")
    for name in ("list_signals", "list_inbox", "list_campaigns", "get_credit_balance"):
        out = await _call(client, token, name)
        assert out.ok, (name, out.message)
    balance = await _call(client, token, "get_credit_balance")
    assert "balance" in balance.data


async def test_a_contact_can_be_fetched_and_drafted_to(client):
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.models.account import Contact
    from tests.conftest import seed_relevance_profile

    token = await signup(client, slug="tl4", email="o@tl4.com")
    p = principal_from_token(token)
    acc = (await _call(client, token, "create_account", name="Hooli",
                       domain="hooli.com")).data["account"]["id"]
    async with get_sessionmaker()() as s:
        ts = TenantSession(s, p.tenant_id)
        await seed_relevance_profile(ts)
        c = Contact(tenant_id=p.tenant_id, account_id=acc, full_name="Gavin Belson",
                    title="CEO", email="gavin@hooli.com")
        ts.add(c)
        await s.commit()
        contact_id = c.id
    got = await _call(client, token, "fetch", id=f"contact:{contact_id}")
    assert got.ok and got.data["contact"]["full_name"] == "Gavin Belson"
    draft = await _call(client, token, "draft_email", contact_id=contact_id)
    assert draft.ok, draft.message
    assert draft.data["body"] and "subject" in draft.data


async def test_a_missing_record_is_a_message_not_a_crash(client):
    token = await signup(client, slug="tl5", email="o@tl5.com")
    out = await _call(client, token, "fetch", id="account:nope123")
    assert not out.ok and out.status == 404


async def test_get_contact_endpoint_is_tenant_scoped(client):
    a = await signup(client, slug="tl6a", email="o@tl6a.com")
    b = await signup(client, slug="tl6b", email="o@tl6b.com")
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession
    from nexus.models.account import Contact

    pa = principal_from_token(a)
    acc = (await client.post("/api/accounts", headers=auth(a), json={"name": "A Co"})).json()["id"]
    async with get_sessionmaker()() as s:
        ts = TenantSession(s, pa.tenant_id)
        c = Contact(tenant_id=pa.tenant_id, account_id=acc, full_name="Ann")
        ts.add(c)
        await s.commit()
        cid = c.id
    assert (await client.get(f"/api/contacts/{cid}", headers=auth(a))).status_code == 200
    assert (await client.get(f"/api/contacts/{cid}", headers=auth(b))).status_code == 404
    # The export route is still the export, not a contact called "export".
    assert (await client.get("/api/contacts/export", headers=auth(a))).status_code == 200


# ---- Prospector (decision D3: browsing masked is free; saving is charged per field) ---------------

@pytest.fixture
def prospect_db():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient
    from tests.prospector_fixtures import FakeVendor

    fake = FakeVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


async def test_prospector_search_returns_masked_rows_only(client, prospect_db):
    from tests.prospector_fixtures import SECRETS

    token = await signup(client, slug="tl7", email="o@tl7.com")
    out = await _call(client, token, "prospector_search", view="contacts",
                      filters={"titles": ["VP Sales"]})
    assert out.ok, out.message
    rows = out.data["rows"]
    assert rows and rows[0]["email_masked"] == "j•••@a•••.com"
    text = str(out.data).lower()
    assert not [s for s in SECRETS if s.lower() in text], "unmasked values reached the model"


async def test_prospector_save_delivers_charged_records_and_returns_ids(client, prospect_db):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_rates()
    token = await signup(client, slug="tl8", email="o@tl8.com")
    found = await _call(client, token, "prospector_search", view="contacts")
    rows = [{k: r[k] for k in ("api_page", "api_page_size", "row_key")}
            for r in found.data["rows"][:2]]
    saved = await _call(client, token, "prospector_save", view="contacts", rows=rows,
                        request_id="mcp-save-0001")
    assert saved.ok, saved.message
    assert saved.data["created"] == 2 and len(saved.data["record_ids"]) == 2
    assert saved.data["charged"] == {"prospector.email": 2}
    again = await _call(client, token, "prospector_save", view="contacts", rows=rows,
                        request_id="mcp-save-0001")
    assert again.ok and again.data["charged"] == {}


async def test_prospector_save_needs_rows_or_a_limit(client, prospect_db):
    token = await signup(client, slug="tl9", email="o@tl9.com")
    out = await _call(client, token, "prospector_save", view="contacts")
    assert not out.ok and "rows" in out.message


async def test_a_provider_outage_reaches_the_model_in_the_apps_own_words(client, prospect_db):
    """2026-10-19, found on the local deploy: InfoJoy answering 500 became "could not finish
    this", so the assistant could not tell the user to retry later. A 503 the app wrote itself
    carries its sentence; the reference is kept for support."""
    prospect_db.status = 500
    token = await signup(client, slug="tl10", email="o@tl10.com")
    out = await _call(client, token, "prospector_search", view="contacts",
                      filters={"titles": ["VP Sales"]})
    assert not out.ok and out.status == 503
    assert "data provider failed" in out.message and "Reference:" in out.message


async def test_an_unexpected_crash_still_says_nothing_internal():
    """A real 500 keeps the generic sentence: its body is whatever the framework produced."""
    async def boom(scope, receive, send):
        await send({"type": "http.response.start", "status": 500,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": b'{"detail": "Traceback (most recent call last): secret"}'})

    gw = Gateway(boom, credentials.Actor(
        user_id="u", tenant_id="t", role="rep", token_version=0, connection_id="c",
        kind="api_key", scopes=frozenset(credentials.SCOPES), name="n"))
    out = await gw.get("/api/billing/credits")
    assert not out.ok and out.status == 500
    assert "could not finish" in out.message and "Traceback" not in out.message
