# nexus/mcp/tools/enrichment.py
"""Research that improves records already in the workspace: firmographics, a re-checked email, a
direct phone number, a fresh crawl for buying signals, and people like a given contact. Each spends
credits exactly as the matching button in the app does; the server checks the balance first."""
from __future__ import annotations

from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools.common import ID, fail, link, obj, ok
from nexus.mcp.tools.registry import REQUEST_ID, ToolSpec


async def _enrich_account(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/accounts/{args['account_id']}/enrich",
                        idempotency_key=args.get("request_id"))
    return ok({"account": out.data}) if out.ok else fail(out)


async def _verify_email(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/accounts/contacts/{args['contact_id']}/reverify",
                        idempotency_key=args.get("request_id"))
    return ok({"result": out.data}) if out.ok else fail(out)


async def _find_phone(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/contacts/{args['contact_id']}/enrich-phone",
                        idempotency_key=args.get("request_id"))
    return ok({"result": out.data}) if out.ok else fail(out)


async def _refresh_account(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/agents/pipeline/{args['account_id']}",
                        idempotency_key=args.get("request_id"))
    if not out.ok:
        return fail(out)
    return ok({"result": out.data, "url": link(f"/accounts/{args['account_id']}"),
               "note": "New signals are on the account: list_signals with its account_id "
                       "reads them."})


async def _find_similar_people(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/accounts/contacts/{args['contact_id']}/lookalikes",
                        params={"limit": args["limit"], "mode": args["mode"]},
                        idempotency_key=args.get("request_id"))
    if not out.ok:
        return fail(out)
    return ok({"mode": args["mode"], "result": out.data,
               "note": "Nothing was added. create_account adds a company; people found here "
                       "are added from the app's Similar people panel."})


RESEARCH: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="enrich_account", title="Enrich an account", scope="research",
        description="Fill in a company's firmographics (industry, size, location, revenue, tech "
                    "stack, LinkedIn page) from public sources and save them to the account.",
        input_schema=obj({"account_id": ID, "request_id": REQUEST_ID}, ("account_id",)),
        handler=_enrich_account, price_capability="enrich.account", price_unit="per account",
        calls=(("POST", "/api/accounts/{id}/enrich"),),
    ),
    ToolSpec(
        name="verify_email", title="Verify a contact's email", scope="research",
        description="Re-check a contact's saved email with a live mailbox check. A valid, risky "
                    "or catch-all address is kept; an invalid or missing one starts a search for "
                    "the right address, charged as enriching a contact instead.",
        input_schema=obj({"contact_id": ID, "request_id": REQUEST_ID}, ("contact_id",)),
        handler=_verify_email, price_capability="verify.email", price_unit="per check",
        calls=(("POST", "/api/accounts/contacts/{id}/reverify"),),
    ),
    ToolSpec(
        name="find_phone", title="Find a contact's phone number", scope="research",
        description="Look up a contact's direct phone number and save it to the contact. A number "
                    "already found is answered without a new search.",
        input_schema=obj({"contact_id": ID, "request_id": REQUEST_ID}, ("contact_id",)),
        handler=_find_phone, price_capability="enrich.phone", price_unit="per contact",
        calls=(("POST", "/api/contacts/{id}/enrich-phone"),),
    ),
    ToolSpec(
        name="refresh_account", title="Refresh an account now", scope="research",
        description="Collect the latest buying signals for an account now (news, hiring, funding, "
                    "the company's own site) and re-score it, instead of waiting for its scheduled "
                    "refresh. Can take a minute.",
        input_schema=obj({"account_id": ID, "request_id": REQUEST_ID}, ("account_id",)),
        handler=_refresh_account, price_capability="workflow.orchestration_run",
        price_unit="per refresh", calls=(("POST", "/api/agents/pipeline/{id}"),),
    ),
    ToolSpec(
        name="find_similar_people", title="Find people similar to a contact", scope="research",
        description="People who resemble a contact (role, seniority, kind of company). mode "
                    "'existing' ranks the workspace's own contacts and is free; mode 'new' finds "
                    "people outside the workspace and is charged for each one returned.",
        input_schema=obj({
            "contact_id": ID,
            "mode": {"type": "string", "enum": ["existing", "new"], "default": "existing"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            "request_id": REQUEST_ID,
        }, ("contact_id",)),
        handler=_find_similar_people, price_capability="discovery.lookalike_contact",
        price_unit="for each new person returned (mode 'new' only)",
        calls=(("POST", "/api/accounts/contacts/{id}/lookalikes"),),
    ),
)
