# nexus/mcp/tools/catalog.py
"""The tools an AI assistant can use, in the fixed order ``tools/list`` returns them.

Each handler is a thin mapping onto endpoints the app already has (``gateway`` explains why).
Descriptions are written for a model choosing between tools: what it does, what it returns, what it
costs, and what it never does. They name no vendor: these words are shown to customers in Claude and
ChatGPT.

Never here: sending email, launching or approving campaigns, deleting, archiving, merging, changing
owners, billing, settings, members, integrations. Those stay in the app (spec 2026-10-13).
"""
from __future__ import annotations

from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools.registry import ID_PATTERN, REQUEST_ID, ToolSpec
from nexus.mcp.brand import PRODUCT_NAME

_ID = {"type": "string", "pattern": ID_PATTERN}
_IDS = {"type": "array", "items": {"type": "string", "pattern": ID_PATTERN}, "maxItems": 500}


def _obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    schema: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


def _base_url() -> str:
    from nexus.core.config import get_settings

    return (get_settings().app_base_url or "").rstrip("/")


def _link(path: str) -> str | None:
    base = _base_url()
    return f"{base}{path}" if base else None


def _fail(out: ToolOutcome) -> ToolOutcome:
    return ToolOutcome(False, None, out.message, out.status)


def _ok(data) -> ToolOutcome:
    return ToolOutcome(True, data)


# ---- read ------------------------------------------------------------------------------------

async def _search(gw: Gateway, args: dict) -> ToolOutcome:
    query, limit = args["query"], args["limit"]
    accounts = await gw.get("/api/accounts", {"q": query, "limit": limit})
    if not accounts.ok:
        return _fail(accounts)
    contacts = await gw.get("/api/contacts", {"q": query, "limit": limit})
    if not contacts.ok:
        return _fail(contacts)
    results = []
    for a in accounts.data or []:
        results.append({
            "id": f"account:{a['id']}", "type": "account", "title": a.get("name"),
            "subtitle": " · ".join(x for x in (a.get("domain"), a.get("industry")) if x),
            "fit_score": a.get("fit_score"), "url": _link(f"/accounts/{a['id']}"),
        })
    for c in contacts.data or []:
        results.append({
            "id": f"contact:{c['id']}", "type": "contact", "title": c.get("full_name"),
            "subtitle": " · ".join(x for x in (c.get("title"), c.get("account_name")) if x),
            "url": _link(f"/accounts/{c['account_id']}") if c.get("account_id") else None,
        })
    return _ok({"query": query, "results": results})


async def _fetch(gw: Gateway, args: dict) -> ToolOutcome:
    kind, _, rid = args["id"].partition(":")
    if kind == "account":
        account = await gw.get(f"/api/accounts/{rid}")
        if not account.ok:
            return _fail(account)
        signals = await gw.get("/api/signals", {"account_id": rid, "limit": 10})
        contacts = await gw.get(f"/api/accounts/{rid}/contacts")
        return _ok({
            "type": "account", "account": account.data,
            "signals": signals.data if signals.ok else [],
            "contacts": contacts.data if contacts.ok else [],
            "url": _link(f"/accounts/{rid}"),
        })
    if kind == "contact":
        contact = await gw.get(f"/api/contacts/{rid}")
        if not contact.ok:
            return _fail(contact)
        account_id = (contact.data or {}).get("account_id")
        return _ok({"type": "contact", "contact": contact.data,
                    "url": _link(f"/accounts/{account_id}") if account_id else None})
    plist = await gw.get(f"/api/lists/{rid}")
    if not plist.ok:
        return _fail(plist)
    members = await gw.get(f"/api/lists/{rid}/members", {"limit": 100})
    return _ok({"type": "list", "list": plist.data,
                "members": (members.data or {}).get("items", []) if members.ok else [],
                "url": _link(f"/lists/{rid}")})


async def _list_signals(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.get("/api/signals", {
        "account_id": args.get("account_id"), "kind": args.get("kind"),
        "max_age_days": args.get("max_age_days"), "min_strength": args.get("min_strength"),
        "limit": args["limit"],
    })
    return _ok({"signals": out.data}) if out.ok else _fail(out)


async def _list_inbox(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.get("/api/inbox", {"mine": str(args["mine"]).lower(), "status": args["status"],
                                      "limit": args["limit"]})
    return _ok({"tasks": out.data}) if out.ok else _fail(out)


async def _list_lists(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.get("/api/lists")
    return _ok({"lists": out.data}) if out.ok else _fail(out)


async def _list_campaigns(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.get("/api/engagement/campaigns")
    return _ok({"campaigns": out.data}) if out.ok else _fail(out)


async def _credit_balance(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.get("/api/billing/credits")
    if not out.ok:
        return _fail(out)
    data = out.data or {}
    return _ok({"balance": data.get("balance"), "recent": (data.get("entries") or [])[:10]})


# ---- research --------------------------------------------------------------------------------

async def _find_contacts(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/accounts/{args['account_id']}/source-contacts",
                        params={"limit": args["limit"]}, idempotency_key=args.get("request_id"))
    if not out.ok:
        return _fail(out)
    found = out.data or []
    return _ok({"added": found, "count": len(found),
                "note": f"Found people are saved to the account in {PRODUCT_NAME}, as Find contacts does."})


async def _find_similar(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/accounts/{args['account_id']}/lookalikes",
                        params={"limit": args["limit"]}, idempotency_key=args.get("request_id"))
    if not out.ok:
        return _fail(out)
    data = out.data or {}
    return _ok({"seed_domain": data.get("seed_domain"), "companies": data.get("lookalikes", []),
                "note": "Nothing was added. Use create_account to add a company you want."})


async def _enrich_contact(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/accounts/contacts/{args['contact_id']}/enrich",
                        idempotency_key=args.get("request_id"))
    return _ok({"contact": out.data}) if out.ok else _fail(out)


def _agent_output(out: ToolOutcome) -> ToolOutcome:
    if not out.ok:
        return _fail(out)
    output = (out.data or {}).get("output") or {}
    if isinstance(output, dict) and output.get("error"):
        return ToolOutcome(False, None, str(output.get("detail") or output.get("error")), 422)
    return _ok(output)


async def _research_account(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post("/api/agents/research/run",
                        json={"account_id": args["account_id"], "inputs": {}},
                        idempotency_key=args.get("request_id"))
    return _agent_output(out)


async def _draft_email(gw: Gateway, args: dict) -> ToolOutcome:
    contact = await gw.get(f"/api/contacts/{args['contact_id']}")
    if not contact.ok:
        return _fail(contact)
    inputs = {"contact_id": args["contact_id"]}
    if args.get("guidance"):
        inputs["guidance"] = args["guidance"]
    out = await gw.post("/api/agents/messaging/run",
                        json={"account_id": contact.data.get("account_id"), "inputs": inputs},
                        idempotency_key=args.get("request_id"))
    result = _agent_output(out)
    if result.ok and isinstance(result.data, dict):
        result.data = {"contact_id": args["contact_id"], "subject": result.data.get("subject"),
                       "body": result.data.get("body"),
                       "note": "Not sent and not saved. save_email_draft puts it in the user's "
                               "Drafts folder."}
    return result


# ---- write -----------------------------------------------------------------------------------

async def _create_account(gw: Gateway, args: dict) -> ToolOutcome:
    body = {k: args[k] for k in ("name", "domain", "industry", "country") if args.get(k)}
    out = await gw.post("/api/accounts", json=body)
    return _ok({"account": out.data, "url": _link(f"/accounts/{out.data['id']}")}) if out.ok \
        else _fail(out)


async def _create_list(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post("/api/lists", json={
        "name": args["name"], "kind": args["kind"],
        "account_ids": args.get("account_ids") or [], "contact_ids": args.get("contact_ids") or [],
    })
    return _ok({"list": out.data}) if out.ok else _fail(out)


async def _add_to_list(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/lists/{args['list_id']}/members", json={
        "account_ids": args.get("account_ids") or [], "contact_ids": args.get("contact_ids") or [],
    })
    return _ok(out.data) if out.ok else _fail(out)


async def _add_to_campaign(gw: Gateway, args: dict) -> ToolOutcome:
    body = {"contact_ids": args.get("contact_ids") or [],
            "account_ids": args.get("account_ids") or []}
    if args.get("list_id"):
        body["list_id"] = args["list_id"]
    out = await gw.post(f"/api/engagement/campaigns/{args['campaign_id']}/contacts", json=body)
    if not out.ok:
        return _fail(out)
    data = dict(out.data or {})
    data["note"] = (f"Nothing was sent. Drafts wait for review in {PRODUCT_NAME} before "
                    "anything goes out.")
    return _ok(data)


async def _complete_task(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/inbox/{args['task_id']}/complete")
    return _ok({"task": out.data}) if out.ok else _fail(out)


async def _save_draft(gw: Gateway, args: dict) -> ToolOutcome:
    out = await gw.post(f"/api/contacts/{args['contact_id']}/save-draft",
                        json={"subject": args.get("subject") or "", "body": args["body"]})
    if not out.ok:
        return _fail(out)
    data = dict(out.data or {})
    data["note"] = "Saved to the user's Drafts folder. Not sent."
    return _ok(data)


TOOLS: tuple[ToolSpec, ...] = (
    # ---- read ----
    ToolSpec(
        name="search", title="Search accounts and contacts", scope="read",
        description="Find accounts (companies) and contacts (people) in the workspace by name, "
                    "domain, title, email or company. Returns ids like 'account:<id>' and "
                    "'contact:<id>' to pass to fetch.",
        input_schema=_obj({
            "query": {"type": "string", "minLength": 1, "maxLength": 200,
                      "description": "Words to look for."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        }, ("query",)),
        handler=_search, calls=(("GET", "/api/accounts"), ("GET", "/api/contacts")),
    ),
    ToolSpec(
        name="fetch", title="Get an account, contact or list", scope="read",
        description="Everything about one record by its id from search: an account with its fit "
                    "score, recent buying signals and contacts; a contact with their company and "
                    "email status; or a list with its members.",
        input_schema=_obj({
            "id": {"type": "string", "pattern": "^(account|contact|list):[A-Za-z0-9_-]{1,64}$",
                   "description": "'account:<id>', 'contact:<id>' or 'list:<id>'."},
        }, ("id",)),
        handler=_fetch,
        calls=(("GET", "/api/accounts/{id}"), ("GET", "/api/signals"),
               ("GET", "/api/accounts/{id}/contacts"), ("GET", "/api/contacts/{id}"),
               ("GET", "/api/lists/{id}"), ("GET", "/api/lists/{id}/members")),
    ),
    ToolSpec(
        name="list_signals", title="List buying signals", scope="read",
        description="Recent buying signals (funding, hiring, leadership changes, launches, news), "
                    "newest first, for the workspace or one account. Signal titles and summaries "
                    "come from the public web.",
        input_schema=_obj({
            "account_id": {**_ID, "description": "Only this account's signals."},
            "kind": {"type": "string", "pattern": "^[a-z_]{1,32}$",
                     "description": "One kind, e.g. funding or hiring."},
            "max_age_days": {"type": "integer", "minimum": 1, "maximum": 365},
            "min_strength": {"type": "number", "minimum": 0, "maximum": 1,
                             "description": "0.5 hides weak mentions."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        }),
        handler=_list_signals, calls=(("GET", "/api/signals"),),
    ),
    ToolSpec(
        name="list_inbox", title="List inbox tasks", scope="read",
        description="The prioritized follow-ups in the inbox: what to do next and why.",
        input_schema=_obj({
            "mine": {"type": "boolean", "default": True,
                     "description": "Only tasks assigned to the user (and unassigned ones)."},
            "status": {"type": "string", "enum": ["open", "done", "snoozed", "all"],
                       "default": "open"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        }),
        handler=_list_inbox, calls=(("GET", "/api/inbox"),),
    ),
    ToolSpec(
        name="list_lists", title="List saved lists", scope="read",
        description="The workspace's saved lists of accounts or contacts, with their sizes.",
        input_schema=_obj({}), handler=_list_lists, calls=(("GET", "/api/lists"),),
    ),
    ToolSpec(
        name="list_campaigns", title="List campaigns", scope="read",
        description="The user's outreach campaigns and their status, to choose one for "
                    "add_contacts_to_campaign.",
        input_schema=_obj({}), handler=_list_campaigns,
        calls=(("GET", "/api/engagement/campaigns"),),
    ),
    ToolSpec(
        name="get_credit_balance", title="Get credit balance", scope="read",
        description="The workspace's credit balance and its most recent movements. Research tools "
                    "spend credits.",
        input_schema=_obj({}), handler=_credit_balance, calls=(("GET", "/api/billing/credits"),),
    ),
    # ---- research ----
    ToolSpec(
        name="find_contacts", title="Find contacts at an account", scope="research",
        description="Find the buying committee at an account: new people matching the "
                    "workspace's target titles, with verified emails where one can be found. Found "
                    "people are saved to the account.",
        input_schema=_obj({
            "account_id": _ID,
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
            "request_id": REQUEST_ID,
        }, ("account_id",)),
        handler=_find_contacts, price_capability="enrich.contact",
        price_unit="for each contact found",
        calls=(("POST", "/api/accounts/{id}/source-contacts"),),
    ),
    ToolSpec(
        name="find_similar_companies", title="Find similar companies", scope="research",
        description="Companies similar to an account, scored against the workspace's ideal "
                    "customer profile. Nothing is added; use create_account for the ones to keep.",
        input_schema=_obj({
            "account_id": _ID,
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "request_id": REQUEST_ID,
        }, ("account_id",)),
        handler=_find_similar, price_capability="discovery.lookalike_company",
        price_unit="for each company returned",
        calls=(("POST", "/api/accounts/{id}/lookalikes"),),
    ),
    ToolSpec(
        name="enrich_contact", title="Enrich a contact", scope="research",
        description="Find and verify a contact's work email (and phone where available) and save "
                    "it to the contact.",
        input_schema=_obj({"contact_id": _ID, "request_id": REQUEST_ID}, ("contact_id",)),
        handler=_enrich_contact, price_capability="enrich.contact", price_unit="per contact",
        calls=(("POST", "/api/accounts/contacts/{id}/enrich"),),
    ),
    ToolSpec(
        name="research_account", title="Research an account", scope="research",
        description="A research brief on an account: what the company does, recent signals, and "
                    "talking points tied to the workspace's value propositions.",
        input_schema=_obj({"account_id": _ID, "request_id": REQUEST_ID}, ("account_id",)),
        handler=_research_account, price_capability="ai.research_brief", price_unit="per brief",
        calls=(("POST", "/api/agents/research/run"),),
    ),
    ToolSpec(
        name="draft_email", title="Draft an email to a contact", scope="research",
        description="Write a short cold email to a contact from their company's signals and the "
                    "workspace's value propositions and email style. Returns subject and body. "
                    "Nothing is sent or saved.",
        input_schema=_obj({
            "contact_id": _ID,
            "guidance": {"type": "string", "maxLength": 1000,
                         "description": "Optional angle or instruction for this draft."},
            "request_id": REQUEST_ID,
        }, ("contact_id",)),
        handler=_draft_email, price_capability="ai.email_draft", price_unit="per draft",
        calls=(("GET", "/api/contacts/{id}"), ("POST", "/api/agents/messaging/run")),
    ),
    # ---- write ----
    ToolSpec(
        name="create_account", title="Add an account", scope="write",
        description="Add a company to the workspace. Give its website domain whenever known: the "
                    "domain is how the product tells companies apart and collects their signals.",
        input_schema=_obj({
            "name": {"type": "string", "minLength": 1, "maxLength": 255},
            "domain": {"type": "string", "maxLength": 255, "description": "e.g. acme.com"},
            "industry": {"type": "string", "maxLength": 120},
            "country": {"type": "string", "maxLength": 80},
        }, ("name",)),
        handler=_create_account, calls=(("POST", "/api/accounts"),),
    ),
    ToolSpec(
        name="create_list", title="Create a list", scope="write",
        description="Create a saved list of accounts or of contacts, optionally with its first "
                    "members.",
        input_schema=_obj({
            "name": {"type": "string", "minLength": 1, "maxLength": 120},
            "kind": {"type": "string", "enum": ["account", "contact"], "default": "account"},
            "account_ids": _IDS, "contact_ids": _IDS,
        }, ("name",)),
        handler=_create_list, calls=(("POST", "/api/lists"),),
    ),
    ToolSpec(
        name="add_to_list", title="Add to a list", scope="write",
        description="Add accounts to an account list, or contacts to a contact list.",
        input_schema=_obj({"list_id": _ID, "account_ids": _IDS, "contact_ids": _IDS},
                          ("list_id",)),
        handler=_add_to_list, idempotent=True, calls=(("POST", "/api/lists/{id}/members"),),
    ),
    ToolSpec(
        name="add_contacts_to_campaign", title="Add contacts to a campaign", scope="write",
        description="Add people to a campaign: named contacts, everyone with an email at named "
                    "accounts, or a whole list. Nothing is sent: drafts wait for the user's review "
                    "in the app.",
        input_schema=_obj({
            "campaign_id": _ID, "contact_ids": _IDS, "account_ids": _IDS, "list_id": _ID,
        }, ("campaign_id",)),
        handler=_add_to_campaign, calls=(("POST", "/api/engagement/campaigns/{id}/contacts"),),
    ),
    ToolSpec(
        name="complete_inbox_task", title="Complete an inbox task", scope="write",
        description="Mark an inbox task done.",
        input_schema=_obj({"task_id": _ID}, ("task_id",)),
        handler=_complete_task, idempotent=True, calls=(("POST", "/api/inbox/{id}/complete"),),
    ),
    ToolSpec(
        name="save_email_draft", title="Save an email to Drafts", scope="write",
        description="Put an email to a contact in the user's own mailbox Drafts folder, signed as "
                    "they sign, for them to review and send. Never sends.",
        input_schema=_obj({
            "contact_id": _ID,
            "subject": {"type": "string", "maxLength": 300},
            "body": {"type": "string", "minLength": 1, "maxLength": 20000},
        }, ("contact_id", "body")),
        handler=_save_draft, calls=(("POST", "/api/contacts/{id}/save-draft"),),
    ),
)

_BY_NAME = {t.name: t for t in TOOLS}


def by_name(name: str) -> ToolSpec | None:
    return _BY_NAME.get(name)
