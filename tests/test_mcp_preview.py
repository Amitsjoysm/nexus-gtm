# tests/test_mcp_preview.py
"""An optional preview for every tool that spends credits or changes something that matters, and
the credits a paid call spent (owner, 2026-11-20: "ask permission and give credit info at each
step", and "ensure all other MCP functions work as intended previously")."""
from __future__ import annotations

import json

import pytest

from nexus.mcp import credentials, protocol
from nexus.mcp.gateway import ToolOutcome, path_allowed
from nexus.mcp.schema import validate
from nexus.mcp.server import call_tool, run_tool
from nexus.mcp.tools import TOOLS, by_name, describe
from nexus.mcp.tools.registry import ToolSpec
from tests.conftest import signup, tenant_session
from tests.test_mcp_tools import _actor

CONSEQUENTIAL_WRITES = {"save_icp", "add_contacts_to_campaign", "prospector_save",
                        "mark_meeting_booked"}


@pytest.fixture
async def billing_seeded():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


@pytest.fixture
def prospect_db():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient
    from tests.prospector_fixtures import FakeVendor

    fake = FakeVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


def _result(payload: dict) -> dict:
    return json.loads(payload["content"][0]["text"])


# ---- shape -----------------------------------------------------------------------------------

def test_every_tool_that_costs_credits_or_matters_can_be_previewed():
    for t in TOOLS:
        wanted = t.costs_credits or t.name in CONSEQUENTIAL_WRITES
        assert t.previewable == wanted, t.name
        assert ("preview" in t.schema()["properties"]) == wanted, t.name


def test_the_preview_argument_is_optional_and_has_no_default():
    for t in TOOLS:
        if t.previewable:
            rule = t.schema()["properties"]["preview"]
            assert rule["type"] == "boolean" and "default" not in rule, t.name
            assert "preview" not in t.schema().get("required", []), t.name


def test_the_listed_schema_is_the_one_a_call_is_checked_against():
    for t in TOOLS:
        assert describe(t, {})["inputSchema"] == t.schema(), t.name
        if t.previewable:
            assert "preview=true" in describe(t, {})["description"], t.name


def test_an_old_call_validates_to_the_same_arguments():
    """No default on `preview`, so an old call's validated arguments carry nothing new."""
    for t in TOOLS:
        sample = {k: v for k, v in _SAMPLES.items() if k in t.input_schema["properties"]
                  and v in t.input_schema["properties"][k].get("enum", [v])}
        assert validate(t.input_schema, sample) == validate(t.schema(), sample), t.name


async def test_a_tool_that_cannot_preview_refuses_the_argument_instead_of_acting():
    actor = credentials.Actor(user_id="u", tenant_id="t", role="owner", token_version=0,
                              connection_id="c", kind="api_key",
                              scopes=frozenset(credentials.SCOPES), name="n")
    with pytest.raises(protocol.ProtocolError) as err:
        await call_tool(None, actor, {"name": "create_account",
                                      "arguments": {"name": "Initech", "preview": True}})
    assert err.value.code == protocol.INVALID_PARAMS and "preview" in str(err.value)


# ---- run_tool against a stand-in gateway ------------------------------------------------------

class _FakeGateway:
    def __init__(self, balances):
        self.balances = list(balances)

    async def get(self, path, params=None):
        assert path == "/api/billing/credits"
        return ToolOutcome(True, {"balance": self.balances.pop(0)})


async def test_without_preview_the_handler_gets_exactly_the_old_arguments():
    seen = []

    async def handler(gw, args):
        seen.append(dict(args))
        return ToolOutcome(True, {"answer": 42, "note": "as before"})

    spec = ToolSpec(name="t", title="T", description="d", scope="research",
                    input_schema={"type": "object", "properties": {}}, handler=handler,
                    price_capability="ai.research_brief", previewable=True)
    outcome = await run_tool(spec, _FakeGateway([100.0, 97.5]), {"account_id": "a1"})
    assert seen == [{"account_id": "a1"}]
    assert outcome.data == {"answer": 42, "note": "as before", "credits_spent": 2.5,
                            "balance_after": 97.5}
    # preview: false is the same call as no preview at all.
    outcome = await run_tool(spec, _FakeGateway([10.0, 10.0]),
                             {"account_id": "a1", "preview": False})
    assert seen[-1] == {"account_id": "a1"} and outcome.data["credits_spent"] == 0


async def test_with_preview_no_handler_runs():
    async def handler(gw, args):
        raise AssertionError("a preview must not act")

    async def preview_fn(gw, args):
        return ToolOutcome(True, {"preview": True, "will": "x"})

    spec = ToolSpec(name="t", title="T", description="d", scope="write",
                    input_schema={"type": "object", "properties": {}}, handler=handler,
                    previewable=True, preview_fn=preview_fn)
    outcome = await run_tool(spec, _FakeGateway([]), {"preview": True})
    assert outcome.data["preview"] is True


async def test_a_free_tool_answers_exactly_what_it_answered():
    async def handler(gw, args):
        return ToolOutcome(True, {"items": [1, 2]})

    spec = ToolSpec(name="t", title="T", description="d", scope="read",
                    input_schema={"type": "object", "properties": {}}, handler=handler)
    outcome = await run_tool(spec, _FakeGateway([]), {})
    assert outcome.data == {"items": [1, 2]}


async def test_a_refused_paid_call_is_returned_untouched():
    refusal = ToolOutcome(False, None, "Not enough credits.", 402)

    async def handler(gw, args):
        return refusal

    spec = ToolSpec(name="t", title="T", description="d", scope="research",
                    input_schema={"type": "object", "properties": {}}, handler=handler,
                    price_capability="ai.research_brief", previewable=True)
    assert await run_tool(spec, _FakeGateway([5.0]), {}) is refusal


_SAMPLES = {"id": "account:abc123", "query": "a", "account_id": "abc123", "contact_id": "abc123",
            "list_id": "abc123", "campaign_id": "abc123", "task_id": "abc123", "name": "n",
            "body": "b", "reply_id": "abc123", "alert_id": "abc123", "kind": "alerts",
            "count": 3, "disposition": "connected", "all_matching_limit": 2}


async def test_a_preview_asks_only_for_quotes_and_never_acts():
    """Whatever a tool does, its preview reads a price and nothing else: no POST but a quote."""
    class Recorder:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def get(self, path, params=None):
            self.sent.append(("GET", path))
            return ToolOutcome(True, {"credits": 4.0, "lines": {}, "uses_credits": True,
                                      "balance": 200.0, "covered": True, "reason": None})

        async def post(self, path, json=None, params=None, idempotency_key=None):
            self.sent.append(("POST", path))
            return ToolOutcome(True, {"records": 2, "total_credits": 2.0, "already_paid": 0,
                                      "balance": 200.0, "enough": True, "uses_credits": True})

    previewable = [t for t in TOOLS if t.previewable]
    assert previewable
    for t in previewable:
        props = t.schema()["properties"]
        args = validate(t.schema(), {**{k: v for k, v in _SAMPLES.items() if k in props
                                        and v in props[k].get("enum", [v])}, "preview": True})
        rec = Recorder()
        out = await run_tool(t, rec, args)  # type: ignore[arg-type]
        assert out.ok and out.data["preview"] is True, (t.name, out.message)
        assert out.data["will"] and out.data["ask_the_user"], t.name
        for method, path in rec.sent:
            assert path_allowed(method, path), (t.name, path)
            assert method == "GET" or path.endswith("/quote"), (t.name, method, path)


async def test_a_preview_states_the_most_the_call_can_cost():
    """find_contacts buys up to `limit`; verify_email can fall through to a contact search."""
    asked = []

    class Quotes:
        async def get(self, path, params=None):
            asked.append(params)
            return ToolOutcome(True, {"credits": 4.0 * params["quantity"], "uses_credits": True,
                                      "balance": 50.0, "covered": True, "reason": None})

    out = await run_tool(by_name("find_contacts"), Quotes(),  # type: ignore[arg-type]
                         {"account_id": "a1", "limit": 7, "preview": True})
    assert asked[-1] == {"capability": "enrich.contact", "quantity": 7}
    assert out.data["max_credits"] == 28.0 and "28 credits" in out.data["ask_the_user"]
    await run_tool(by_name("verify_email"), Quotes(),  # type: ignore[arg-type]
                   {"contact_id": "c1", "preview": True})
    assert asked[-1] == {"capability": "enrich.contact", "quantity": 1}
    free = await run_tool(by_name("find_similar_people"), Quotes(),  # type: ignore[arg-type]
                          {"contact_id": "c1", "mode": "existing", "limit": 10,
                           "preview": True})
    assert free.data["max_credits"] == 0 and free.data["uses_credits"] is False


async def test_a_preview_that_is_not_covered_says_so():
    class Short:
        async def get(self, path, params=None):
            return ToolOutcome(True, {"credits": 25.0, "uses_credits": True, "balance": 3.0,
                                      "covered": False, "reason": "Not enough credits."})

    out = await run_tool(by_name("research_account"), Short(),  # type: ignore[arg-type]
                         {"account_id": "a1", "preview": True})
    assert out.data["covered"] is False
    assert out.data["ask_the_user"].startswith("Tell the user this cannot run: Not enough")


# ---- through the server, on an offline workspace ----------------------------------------------

async def test_a_preview_through_the_server_spends_nothing(client, billing_seeded):
    from sqlalchemy import select

    from nexus.billing.credits import balance
    from nexus.models.account import Account, Contact
    from nexus.models.audit import AuditLog

    token = await signup(client, slug="pv1", email="o@pv1.io", company="PV1")
    actor = _actor(token)
    async with tenant_session(actor.tenant_id) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Globex", domain="globex.com")
        ts.add(acc)
        await ts.flush()
        acc_id = acc.id
        before = await balance(ts)
    out = _result(await call_tool(client._transport.app, actor, {
        "name": "find_contacts", "arguments": {"account_id": acc_id, "preview": True}}))
    assert out["preview"] is True and out["ask_the_user"], out
    assert {"will", "max_credits", "covered", "uses_credits", "balance"} <= set(out)
    assert out["units"] == 5
    async with tenant_session(actor.tenant_id) as ts:
        assert await ts.list(Contact) == [], "a preview finds nobody"
        assert await balance(ts) == before, "and charges nothing"
        audited = (await ts.session.scalars(select(AuditLog).where(
            AuditLog.action == "mcp.tool"))).all()
    assert audited == [], "a preview changed nothing, so it is not audited"


async def test_called_as_before_a_paid_tool_acts_and_reports_credits(client, billing_seeded):
    token = await signup(client, slug="pv2", email="o@pv2.io", company="PV2")
    actor, app = _actor(token), client._transport.app
    made = _result(await call_tool(app, actor, {"name": "create_account",
                                                "arguments": {"name": "Initech"}}))
    assert "credits_spent" not in made, "a free tool's result is untouched"
    asked = _result(await call_tool(app, actor, {"name": "research_account", "arguments": {
        "account_id": made["account"]["id"], "preview": True}}))
    out = _result(await call_tool(app, actor, {"name": "research_account", "arguments": {
        "account_id": made["account"]["id"]}}))
    assert "preview" not in out
    assert "credits_spent" in out and "balance_after" in out
    assert asked["uses_credits"] and asked["max_credits"] > 0
    assert out["credits_spent"] == asked["max_credits"], "the preview named what the call spent"
    assert out["balance_after"] == asked["balance"] - asked["max_credits"]


async def test_prospector_save_previews_its_own_quote_and_saves_nothing(client, billing_seeded,
                                                                        prospect_db):
    from nexus.models.account import Contact

    token = await signup(client, slug="pv3", email="o@pv3.io", company="PV3")
    actor, app = _actor(token), client._transport.app
    found = _result(await call_tool(app, actor, {"name": "prospector_search",
                                                 "arguments": {"view": "contacts"}}))
    rows = found["rows"][:2]
    preview = _result(await call_tool(app, actor, {"name": "prospector_save", "arguments": {
        "view": "contacts", "rows": rows, "preview": True}}))
    assert preview["preview"] is True and preview["quote"]["records"] == 2, preview
    assert preview["will"].startswith("Save 2 prospect records")
    assert preview["max_credits"] == (preview["quote"]["total_credits"]
                                      if preview["uses_credits"] else 0)
    async with tenant_session(actor.tenant_id) as ts:
        assert await ts.list(Contact) == [], "a preview delivers nothing"

    saved = _result(await call_tool(app, actor, {"name": "prospector_save", "arguments": {
        "view": "contacts", "rows": rows, "request_id": "mcp-preview-0001"}}))
    assert saved["created"] == 2 and "preview" not in saved
    assert saved["credits_spent"] == preview["max_credits"]
    assert saved["balance_after"] == preview["balance"] - preview["max_credits"]
