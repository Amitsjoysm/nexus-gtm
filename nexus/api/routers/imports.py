"""Bring existing lists in — by CSV upload, or pulled from the connected CRM.

Distinct from `custom_fields.import_csv`, which *annotates* rows that already match and skips the
rest. These **create**.

Two rules the endpoints exist to enforce:

* **The mapping is explicit.** The operator says which CSV column is the company name and which is
  the website. Guessing from headers is how a "Company" column of parent-company names silently
  becomes the account name for every subsidiary.
* **The count is bounded.** Every imported account enters the refresh pipeline and starts spending
  credits, so an import that quietly pulls a 100,000-row CRM is a bill nobody agreed to.
"""
from __future__ import annotations

import logging

import json

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field

from nexus.api.deps import Permission, Principal, get_tenant_session, require
from nexus.core.tenancy import TenantSession
from nexus.imports.crm_pull import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    clamp_limit,
    import_accounts_from_crm,
    import_contacts_from_crm,
)
from nexus.imports.csv_ingest import (
    ACCOUNT_INT_FIELDS,
    ACCOUNT_LIST_FIELDS,
    ACCOUNT_TEXT_FIELDS,
    CONTACT_TEXT_FIELDS,
    CUSTOM_PREFIX,
    import_accounts_csv,
    import_contacts_csv,
)

router = APIRouter(prefix="/imports", tags=["imports"])

logger = logging.getLogger("nexus.api.routers.imports")

# 20 MB. A 50,000-row CSV of accounts is roughly 5 MB, so this is generous for the real case while
# still refusing an upload that would sit in memory.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# The fields a CSV column may be mapped onto, surfaced so the UI can build the mapping picker from
# the server rather than from a hard-coded list that drifts.
ACCOUNT_FIELDS = (
    *ACCOUNT_TEXT_FIELDS, "domain", *ACCOUNT_INT_FIELDS, *ACCOUNT_LIST_FIELDS,
)
CONTACT_FIELDS = ("full_name", "email", *CONTACT_TEXT_FIELDS, "account_domain", "account_name")


class ImportResult(BaseModel):
    created: int
    updated: int
    skipped: int
    total_rows: int
    errors: list[str]
    #: The rows this import created or updated, for the actions offered after it. At most
    #: `MAX_RETURNED_IDS`; a CRM pull returns none.
    account_ids: list[str] = []
    contact_ids: list[str] = []


class CustomFieldOut(BaseModel):
    """One of the workspace's own field definitions, offered as a mapping target."""

    #: The value to send as the mapping target, already prefixed: `custom:territory`.
    target: str
    key: str
    label: str
    kind: str


class RequiredFieldOut(BaseModel):
    field: str
    #: What a value needs, the same sentence the sample file's guide line carries.
    note: str


class ImportFieldsOut(BaseModel):
    account_fields: list[str]
    contact_fields: list[str]
    #: The required fields and what each needs, from the sample file's own guide
    #: (`nexus/imports/template.py`), so the screen and the file cannot say different things.
    account_required: list[RequiredFieldOut] = []
    contact_required: list[RequiredFieldOut] = []
    #: This workspace's own `custom_field_defs`. Empty is normal and means the picker offers only
    #: the built-in columns; an unmapped CSV column is still kept, under its own header.
    account_custom_fields: list[CustomFieldOut] = []
    contact_custom_fields: list[CustomFieldOut] = []
    max_rows: int
    default_limit: int
    max_upload_bytes: int
    #: The target that means "drop this column". Sent rather than hardcoded in the client, so the
    #: sentinel the picker offers and the one the server honours cannot drift apart.
    skip_target: str = "__skip__"


async def _custom_targets(ts: TenantSession, entity: str) -> list[CustomFieldOut]:
    """The workspace's defined fields for one entity, as mapping targets.

    Read per request rather than cached: a user who has just defined a field and come straight to
    the import screen must see it, and this is one indexed read on a table with a handful of rows.
    """
    from nexus.models.chat import CustomFieldDef

    rows = await ts.list(CustomFieldDef, CustomFieldDef.entity == entity, limit=200)
    return [
        CustomFieldOut(
            target=f"{CUSTOM_PREFIX}{r.key}", key=r.key, label=r.label or r.key, kind=r.kind
        )
        for r in sorted(rows, key=lambda r: (r.label or r.key).lower())
    ]


async def _allowed_targets(
    ts: TenantSession, entity: str, builtin: tuple[str, ...]
) -> set[str]:
    """Built-in columns plus this workspace's own `custom:` targets.

    Resolved against the DEFINITIONS, not against whatever the client sent: accepting any
    `custom:<anything>` would turn the prefix into a way to write arbitrary keys into
    `custom_fields`, which is the namespace the workspace's own filters read.
    """
    return set(builtin) | {c.target for c in await _custom_targets(ts, entity)}


def _parse_mapping(raw: str, allowed: set[str]) -> dict[str, str]:
    from nexus.imports.csv_ingest import SKIP_TARGET

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid mapping: {exc}")
    if not isinstance(parsed, dict) or not parsed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "mapping must be a non-empty JSON object of {csv_column: field}")
    # "Drop this column" is a legitimate target, and the only way to say it: an UNMAPPED column is
    # kept under its own header, which is the right default and leaves no way to exclude anything.
    unknown = sorted({v for v in parsed.values() if v not in allowed and v != SKIP_TARGET})
    if unknown:
        # Named rather than ignored: a typo'd target silently drops that column, and the operator
        # discovers it only by noticing the data is missing later.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown target field(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}",
        )
    return {str(k): str(v) for k, v in parsed.items()}


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    if not content.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the file is empty")
    return content


@router.get("/fields", response_model=ImportFieldsOut)
async def importable_fields(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportFieldsOut:
    """What a CSV column can be mapped onto. The mapping UI builds itself from this.

    Tenant-scoped, because half the answer is the workspace's own `custom_field_defs`. Before that,
    a column could only be mapped onto a built-in column or dropped into `custom_fields` under its
    RAW HEADER — so a workspace that had defined `territory` and uploaded a "Territory (2026)"
    column stored the value under that literal string, where the field it defined never saw it.
    """
    from nexus.imports.csv_ingest import MAX_ROWS, SKIP_TARGET
    from nexus.imports.template import required_notes

    return ImportFieldsOut(
        account_fields=list(ACCOUNT_FIELDS),
        contact_fields=list(CONTACT_FIELDS),
        account_required=[RequiredFieldOut(**n) for n in required_notes("accounts", ACCOUNT_FIELDS)],
        contact_required=[RequiredFieldOut(**n) for n in required_notes("contacts", CONTACT_FIELDS)],
        account_custom_fields=await _custom_targets(ts, "account"),
        contact_custom_fields=await _custom_targets(ts, "contact"),
        max_rows=MAX_ROWS,
        default_limit=DEFAULT_LIMIT,
        max_upload_bytes=MAX_UPLOAD_BYTES,
        skip_target=SKIP_TARGET,
    )


@router.get("/template/{entity}.csv")
async def import_template(
    entity: Literal["accounts", "contacts"],
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> Response:
    """A sample file for the import: every mappable column, a guide line saying which are required
    or recommended and what each needs, and two example rows.

    Built from the same field list `/fields` serves, plus this workspace's own fields, so it can
    never offer a column the import would refuse or leave one out. The headers are the mapping
    targets, so the mapping screen matches each column on sight. The guide line and the examples
    (on documentation-only domains) are skipped on import, so a filled-in copy imports only the
    operator's rows. Free: it is a description of the file, not data.
    """
    from nexus.imports.template import build_template

    builtin = ACCOUNT_FIELDS if entity == "accounts" else CONTACT_FIELDS
    custom = await _custom_targets(ts, "account" if entity == "accounts" else "contact")
    text = build_template(entity, builtin, [(c.target, c.label) for c in custom])
    return Response(
        # A byte-order mark, so Excel opens the file as UTF-8 and a curly quote in a custom field's
        # name does not arrive as mojibake. The importer strips it (`utf-8-sig`).
        content="\ufeff" + text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{entity}-import-template.csv"'},
    )


async def _meter_import(ts, result: dict, *, user_id: str, kind: str) -> None:
    """Charge `data.import_csv` for the rows that actually landed.

    Quantity is rows IMPORTED, not rows in the file: a customer who uploads 5,000 rows of which
    80 are new has bought 80 records, and billing the file size would charge them for their own
    duplicates. A run that imported nothing is not billed at all — the same "nothing bought,
    nothing charged" rule the enrichment seam follows.

    Metered after the parse rather than around it, because the count is not knowable up front;
    enforcement therefore applies to the NEXT import, which is the honest behaviour for a bulk
    job whose size is discovered by doing it.
    """
    from nexus.billing.meter import metered

    # `created`/`updated`, which is what `csv_ingest` returns — NOT `imported`, which it has
    # never returned. Reading the wrong key made this bill zero on every upload: the meter was
    # wired and silently inert, the exact state this audit keeps finding.
    rows = int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
    if rows <= 0:
        return
    # `metered`, NOT `record_usage`. The latter appends a usage event and stops — it resolves no
    # entitlement, checks no quota and burns no credit — so a call site using it records the
    # import and charges nothing. Same trap, one layer down.
    try:
        async with metered(ts, "data.import_csv", quantity=rows, user_id=user_id,
                           attrs={"kind": kind}):
            pass
    except Exception:
        logger.warning("metering failed for data.import_csv", exc_info=True)


@router.post("/accounts/csv", response_model=ImportResult)
async def upload_accounts_csv(
    mapping: str = Form(...),
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    allowed = await _allowed_targets(ts, "account", ACCOUNT_FIELDS)
    result = await import_accounts_csv(
        ts, content=await _read_upload(file), mapping=_parse_mapping(mapping, allowed),
        owner_user_id=principal.user_id,
    )
    await _meter_import(ts, result, user_id=principal.user_id, kind="accounts")
    await ts.commit()
    return ImportResult(**result)


@router.post("/contacts/csv", response_model=ImportResult)
async def upload_contacts_csv(
    mapping: str = Form(...),
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    allowed = await _allowed_targets(ts, "contact", CONTACT_FIELDS)
    result = await import_contacts_csv(
        ts, content=await _read_upload(file), mapping=_parse_mapping(mapping, allowed),
        owner_user_id=principal.user_id,
    )
    await _meter_import(ts, result, user_id=principal.user_id, kind="contacts")
    await ts.commit()
    return ImportResult(**result)


async def _connector_or_400(ts: TenantSession):
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    connector = await resolve_crm_connector(ts)
    if connector is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No CRM is connected for this workspace. Connect one under Settings > Integrations.",
        )
    return connector


@router.post("/accounts/crm", response_model=ImportResult)
async def pull_accounts_from_crm(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    result = await import_accounts_from_crm(ts, await _connector_or_400(ts), limit=clamp_limit(limit))
    await ts.commit()
    return ImportResult(**result)


@router.post("/contacts/crm", response_model=ImportResult)
async def pull_contacts_from_crm(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportResult:
    result = await import_contacts_from_crm(ts, await _connector_or_400(ts), limit=clamp_limit(limit))
    await ts.commit()
    return ImportResult(**result)


# ---- after an import: verify the emails, fill the firmographics ---------------------------------
#
# Offered on the result screen for the rows the import just wrote. Both run the product's existing
# charged work, at its existing price: verification is the one Prospector's Verify uses
# (`prospector.verify`: a fresh verifier, `verify.email` per address, Prospector-delivered ones free,
# up to 25 at once and the rest in the background), and firmographics are the enricher the account
# page's Enrich runs (`enrich.account` per account, metered by the enricher itself). What is new is
# only the choice of rows and the price shown before anything runs.

ImportAction = Literal["verify_emails", "fill_firmographics"]

#: The capability each action charges, per row it acts on.
_ACTION_CAPABILITY = {"verify_emails": "verify.email", "fill_firmographics": "enrich.account"}


class ImportActionIn(BaseModel):
    model_config = {"extra": "forbid"}

    ids: list[str] = Field(min_length=1, max_length=1000)


class ImportActionQuoteIn(ImportActionIn):
    action: ImportAction


class ImportActionQuoteOut(BaseModel):
    action: str
    #: Rows the action would act on: contacts with an address and no verdict yet, or accounts
    #: missing industry, size or country. The rest are left out, and are not charged.
    count: int
    #: Of those, the ones charged (Prospector-delivered addresses verify free).
    charged: int
    credits_per_row: float
    total_credits: float
    balance: float | None = None
    #: What `preflight` says: would the charge for `charged` rows be refused right now?
    allowed: bool
    reason: str = ""


class ImportActionOut(BaseModel):
    action: str
    #: `inline` finished in this request; `queued` runs in the background.
    mode: Literal["inline", "queued"]
    count: int
    charged: int = 0


#: A verdict that is already an answer; these are not checked again.
_DECIDED = ("valid", "catch_all", "invalid", "risky")


async def _verify_targets(ts: TenantSession, ids: list[str]):
    from nexus.models.account import Contact

    rows = await ts.list(Contact, Contact.id.in_(list(dict.fromkeys(ids))),
                         Contact.deleted_at.is_(None))
    return [c for c in rows if (c.email or "").strip() and (c.email_status or "") not in _DECIDED]


async def _firmographic_targets(ts: TenantSession, ids: list[str]):
    from nexus.models.account import Account

    rows = await ts.list(Account, Account.id.in_(list(dict.fromkeys(ids))),
                         Account.archived_at.is_(None))
    return [a for a in rows if not (a.industry and a.employee_count and a.country)]


async def _targets(ts: TenantSession, action: str, ids: list[str]) -> tuple[list, int]:
    """The rows an action acts on, and how many of them are charged."""
    if action == "verify_emails":
        from nexus.prospector.verify import _is_free

        contacts = await _verify_targets(ts, ids)
        return contacts, sum(1 for c in contacts if not _is_free(c))
    accounts = await _firmographic_targets(ts, ids)
    return accounts, len(accounts)


@router.post("/actions/quote", response_model=ImportActionQuoteOut)
async def quote_import_action(
    body: ImportActionQuoteIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportActionQuoteOut:
    """What an after-import action would cost, before it runs. Charges nothing."""
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import preflight
    from nexus.models.billing import BillingRateCard

    capability = _ACTION_CAPABILITY[body.action]
    rows, charged = await _targets(ts, body.action, body.ids)
    card = await ts.session.get(BillingRateCard, capability)
    per = float(card.credits_per_unit or 0) if card is not None and card.active else 0.0
    try:
        held = float(await balance(ts))
    except Exception:
        held = None
    decision = await preflight(ts, capability, quantity=charged) if charged else None
    blocked = decision is not None and not decision.allowed
    return ImportActionQuoteOut(
        action=body.action, count=len(rows), charged=charged, credits_per_row=per,
        total_credits=per * charged, balance=held, allowed=not blocked,
        reason=_refusal(decision) if blocked else "",
    )


def _refusal(decision) -> str:
    """The sentence a person reads when the action would be refused, by why."""
    ent = decision.entitlement
    if ent is not None and getattr(ent, "switch_state", None) not in (None, "enabled"):
        return ent.switch_message or "This is switched off for now."
    if decision.reason == "throttled":
        return "Too many requests just now. Try again in a minute."
    if decision.reason in ("disabled", "dependency"):
        return "Your plan does not include this."
    return "Your credit balance does not cover this."


@router.post("/actions/verify-emails", response_model=ImportActionOut)
async def verify_imported_emails(
    body: ImportActionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportActionOut:
    """Verify the imported contacts' addresses that have no verdict yet. 402 before anything is
    checked when the balance cannot cover them."""
    from nexus.prospector.verify import verify_contacts

    contacts = await _verify_targets(ts, body.ids)
    if not contacts:
        return ImportActionOut(action="verify_emails", mode="inline", count=0)
    out = await verify_contacts(ts, user_id=principal.user_id,
                                contact_ids=[c.id for c in contacts])
    await ts.commit()
    return ImportActionOut(action="verify_emails", mode=out["mode"], count=out["count"],
                           charged=out["charged"])


@router.post("/actions/fill-firmographics", response_model=ImportActionOut)
async def fill_imported_firmographics(
    body: ImportActionIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> ImportActionOut:
    """Fill blank industry, size and country on the imported accounts that lack them, in the
    background. The balance is checked for all of them first (402); each account is then charged
    by the enricher as the account page's Enrich is, so one that finds nothing to buy costs nothing.
    """
    from nexus.billing.entitlements import preflight
    from nexus.workers.tasks import enqueue_fill_firmographics

    accounts = await _firmographic_targets(ts, body.ids)
    if not accounts:
        return ImportActionOut(action="fill_firmographics", mode="queued", count=0)
    (await preflight(ts, "enrich.account", quantity=len(accounts))).raise_if_blocked()
    await enqueue_fill_firmographics(ts.tenant_id, principal.user_id, [a.id for a in accounts])
    return ImportActionOut(action="fill_firmographics", mode="queued", count=len(accounts))
