"""Create accounts and contacts from an uploaded CSV.

The product already had `custom_fields.import_csv`, and it does something different: it *annotates*
rows that already match and **skips** every row that does not. So a team arriving with a list of
companies they already work had no way to get it in — the first blocker a tester reported, and the
one that makes an ops team stop evaluating.

Identity rules mirror `nexus/companies/` and `nexus/people/`, deliberately and for the same reason:
a name match is how two different organisations become one row, and that family of bug has shipped
six times in this codebase.

* An **account** is identified by its NORMALISED DOMAIN. Failing that, by exact name within the
  tenant — which is safe *here* in a way it is not across tenants, because the operator is uploading
  their own list and is the authority on what the names mean.
* A **contact** is identified by NORMALISED EMAIL within the tenant.

Everything runs through the caller's :class:`TenantSession`, so RLS applies unchanged and an import
can never write outside the caller's workspace.
"""
from __future__ import annotations

import csv as _csv
import io

from nexus.models.account import Account, Contact

# A row cap rather than a byte cap, because the byte cap belongs at the HTTP layer where the file
# arrives. This is the guard against a 200-column CSV that is small on disk and enormous in rows.
MAX_ROWS = 50_000

# Fields an operator may map a CSV column onto. Anything else is kept on `custom_fields` rather
# than dropped: an ops CSV always carries columns we have no column for, and those columns are
# usually the reason the list was built — territory, tier, owner, campaign.
#
# `crm_id` / `crm_source` are mappable because an ops CSV is usually a CRM export that carries the
# record id. Without them the import creates a second row for an account the CRM already knows, and
# the next sync pushes a duplicate back.
ACCOUNT_TEXT_FIELDS = (
    "name", "industry", "country", "region", "postal_code", "crm_id", "crm_source",
)
ACCOUNT_INT_FIELDS = ("employee_count", "annual_revenue")

# Multi-value columns. `tech_stack` is mappable because the relevance engine scores on it — an
# imported technographic column that landed in `custom_fields` was data the product had and could
# not read.
ACCOUNT_LIST_FIELDS = ("tech_stack",)

# `full_name` and `email` are handled separately: one is the fallback identity, the other IS the
# identity, so neither can be written blindly in a loop.
CONTACT_TEXT_FIELDS = ("title", "seniority", "phone", "linkedin_url")

# A mapping target naming one of the workspace's OWN custom field definitions, e.g.
# ``custom:territory``.
#
# Without this, every unmapped column lands in ``custom_fields`` keyed by its RAW CSV HEADER — so a
# workspace that had defined a `territory` field and uploaded a file with a "Territory (2026)"
# column got the value stored under that literal string, invisible to every filter and to the field
# it defined. The prefix keeps the two namespaces apart: a custom field keyed `industry` and the
# real `industry` column are different targets and must stay distinguishable.
CUSTOM_PREFIX = "custom:"

#: "Do not import this column at all." Unmapped columns are KEPT, under their own header, because an
#: ops CSV's extra columns are usually why the list was built — but that rule left no way to drop
#: one, and some columns must be dropped: a notes column with somebody's medical leave in it, an
#: internal score, a stale owner. Mapped explicitly rather than by omission, so "I chose to drop
#: this" and "I forgot about this" stay different statements.
SKIP_TARGET = "__skip__"


def split_targets(mapping: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """``{csv_column: target}`` -> (column->real field, column->custom field key).

    A column mapped to `SKIP_TARGET` appears in NEITHER. It still counts as mapped to the caller,
    which is what keeps it out of the extras — that is the whole point of the target.
    """
    real: dict[str, str] = {}
    custom: dict[str, str] = {}
    for column, target in mapping.items():
        if target == SKIP_TARGET:
            continue
        if target.startswith(CUSTOM_PREFIX):
            key = target[len(CUSTOM_PREFIX):].strip()
            if key:
                custom[column] = key
        else:
            real[column] = target
    return real, custom


def _split_list(raw: str) -> list[str]:
    """``'AWS, Snowflake; dbt'`` -> ``['AWS', 'Snowflake', 'dbt']``.

    Comma AND semicolon, because a CSV column holding commas is usually quoted and exported with
    semicolons instead — and an operator who did quote it should not lose the split.
    """
    parts = [p.strip() for chunk in (raw or "").split(";") for p in chunk.split(",")]
    return [p for p in parts if p]


def _decode(content: bytes) -> str:
    """UTF-8, falling back to cp1252.

    Excel on Windows writes cp1252, and that is what a GTM team exports. Letting a
    UnicodeDecodeError escape would reject the single commonest file in this category.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def normalise_domain(raw: str) -> str:
    """``'https://www.Acme.com/pricing'`` -> ``'acme.com'``. ``''`` when there is nothing usable.

    Deliberately the same shape as the normalisation the shared company store keys on. A CSV
    carries every spelling of a URL a human can type, and treating two of them as two companies is
    how a re-import doubles the book.
    """
    value = (raw or "").strip().lower()
    if not value:
        return ""
    value = value.split("://", 1)[-1]
    value = value.split("/", 1)[0].split("?", 1)[0].split("@")[-1].strip()
    if value.startswith("www."):
        value = value[4:]
    return value if "." in value and " " not in value else ""


def normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()


#: Delimiters a spreadsheet actually exports. Excel writes the LIST SEPARATOR of the machine's
#: locale, so a German, French, Dutch, Spanish or Indian export is semicolon-separated and a
#: "Save as: Text (tab delimited)" is tabs. Reading one of those as comma-separated yields exactly
#: one column whose header is the whole line, every row is then skipped for having no company name
#: and no website, and the upload reports "0 created" with nothing wrong on screen.
_DELIMITERS = (",", ";", "\t", "|")


def sniff_delimiter(text: str) -> str:
    """The delimiter of the HEADER line: whichever candidate splits it into the most fields.

    The header rather than the file, because it is the one line guaranteed to be present, to be one
    record, and to carry no free text. Ties go to the comma by ordering, so an ordinary CSV is never
    re-interpreted. Quoted sections are ignored, or a header like `"Revenue, USD",Owner` counts the
    comma inside the quotes.
    """
    header = text.split("\n", 1)[0]
    outside = []
    in_quotes = False
    for char in header:
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            outside.append(char)
    line = "".join(outside)
    return max(_DELIMITERS, key=lambda d: (line.count(d), -_DELIMITERS.index(d)))


def _rows(content: bytes) -> list[dict]:
    text = _decode(content)
    reader = _csv.DictReader(io.StringIO(text), delimiter=sniff_delimiter(text))
    return [row for _, row in zip(range(MAX_ROWS), reader)]


def _extras(row: dict, mapped_columns: set[str]) -> dict:
    return {
        key.strip(): value.strip()
        for key, value in row.items()
        if key and key not in mapped_columns and isinstance(value, str) and value.strip()
    }


def _custom_values(row: dict, custom: dict[str, str], extras: dict) -> dict:
    """Merge explicitly-mapped custom fields over the unmapped-column extras.

    Explicit wins: the operator said this column IS the `territory` field, and a raw header that
    happens to collide must not overwrite the thing they chose.
    """
    merged = dict(extras)
    for column, key in custom.items():
        value = (row.get(column) or "").strip()
        if value:
            merged[key] = value
    return merged


#: Postgres `integer` and `bigint`. SQLite has neither limit, so a value that overflows the column
#: passes every test here and takes down the whole upload in production with a
#: `NumericValueOutOfRange` - one mis-mapped column (revenue typed into Employee count) against one
#: row. Out of range is treated as unparseable, which is what it is.
_INT_MAX = {"employee_count": 2**31 - 1, "annual_revenue": 2**63 - 1}


def _to_int(raw: str, limit: int = 2**63 - 1) -> int | None:
    """Parse '1,200', '$25,000,000' and '25000000'. Returns None for anything else.

    Ops spreadsheets format numbers for humans. Refusing a value with a comma in it would drop the
    revenue column of most real files.
    """
    cleaned = (raw or "").replace(",", "").replace("$", "").replace(" ", "").strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if value <= limit else None


def _row_reason(exc: Exception) -> str:
    """The database's own sentence about this row, trimmed to one line.

    "value too long for type character varying(120)" tells an operator which column to fix;
    "the import failed" tells them to file a ticket. SQLAlchemy wraps the driver error and appends
    the whole statement and its parameters, which would put the row's data - including whatever the
    workspace considers private - into a response and a log line, so only the first line is kept.
    """
    original = getattr(exc, "orig", None) or exc
    text = str(original).strip().splitlines()
    reason = text[0].strip() if text else exc.__class__.__name__
    return reason[:200] or exc.__class__.__name__


def _apply_account(account: Account, fields: dict, extras: dict) -> None:
    """Write mapped fields onto an account.

    A BLANK CSV cell never overwrites a stored value. A partial list — the common case, since a rep
    exports three columns out of the CRM — must not erase firmographics the product already paid an
    enrichment provider for.
    """
    for field in ACCOUNT_TEXT_FIELDS:
        value = (fields.get(field) or "").strip()
        if value:
            setattr(account, field, value)

    domain = normalise_domain(fields.get("domain", ""))
    if domain:
        account.domain = domain

    for field in ACCOUNT_INT_FIELDS:
        parsed = _to_int(fields.get(field, ""), _INT_MAX.get(field, 2**63 - 1))
        if parsed is not None:
            setattr(account, field, parsed)

    for field in ACCOUNT_LIST_FIELDS:
        values = _split_list(fields.get(field, ""))
        if values:
            # UNIONED, not replaced — the same rule the shared company store applies to
            # `tech_stack`, and the same rule as the blank cell above. A three-column CSV listing
            # the two technologies the rep happens to care about must not delete the eleven an
            # enrichment provider was paid to find.
            current = list(getattr(account, field, None) or [])
            seen = {str(v).casefold() for v in current}
            for value in values:
                if value.casefold() not in seen:
                    seen.add(value.casefold())
                    current.append(value)
            setattr(account, field, current)

    if extras:
        account.custom_fields = {**(account.custom_fields or {}), **extras}


async def import_accounts_csv(
    ts, *, content: bytes, mapping: dict[str, str], owner_user_id: str | None = None
) -> dict:
    """Create or update accounts from CSV. ``mapping`` is ``{csv_column: account_field}``.

    ``owner_user_id`` owns the accounts this import CREATES: a person uploading a list is adding
    those accounts, the same as typing them in. Accounts it only updates keep their owner — an
    import is not a way to take a colleague's book.
    """
    created = updated = skipped = 0
    errors: list[str] = []
    rows = _rows(content)
    mapped_columns = set(mapping)
    real, custom = split_targets(mapping)

    for index, row in enumerate(rows, start=2):  # 2 == the first data line in a spreadsheet
        fields = {field: (row.get(column) or "").strip() for column, field in real.items()}
        name = fields.get("name", "")
        domain = normalise_domain(fields.get("domain", ""))

        if not name and not domain:
            skipped += 1
            errors.append(f"row {index}: no company name and no usable website")
            continue

        existing = None
        if domain:
            existing = await ts.first(Account, Account.domain == domain)
        if existing is None and name:
            existing = await ts.first(Account, Account.name == name)

        extras = _custom_values(row, custom, _extras(row, mapped_columns))
        try:
            # ONE ROW, ONE SAVEPOINT. The write used to be flushed straight onto the request's
            # transaction, so a single row the database refused - a value longer than the column,
            # a domain already held by another account - raised out of the loop, rolled the whole
            # upload back and returned a 500. The operator saw "the import failed" for a file that
            # was 4,998 good rows and two bad ones, with nothing naming either.
            async with ts.session.begin_nested():
                if existing is None:
                    account = Account(
                        tenant_id=ts.tenant_id, name=name or domain, source="csv_import",
                        owner_user_id=owner_user_id,
                    )
                    _apply_account(account, fields, extras)
                    ts.add(account)
                    await ts.flush()
                    created += 1
                else:
                    _apply_account(existing, fields, extras)
                    await ts.flush()
                    updated += 1
        except Exception as exc:  # noqa: BLE001 - the database's reason is what the operator needs
            skipped += 1
            errors.append(f"row {index}: {_row_reason(exc)}")

    return {
        "created": created, "updated": updated, "skipped": skipped,
        "total_rows": len(rows), "errors": errors[:50],
    }


async def import_contacts_csv(
    ts, *, content: bytes, mapping: dict[str, str], owner_user_id: str | None = None
) -> dict:
    """Create or update contacts from CSV. Identity is the normalised email within the tenant.

    A contact whose company is not in the book yet **creates** the account. Refusing would make the
    two imports order-dependent for a reason invisible from the upload screen, and a contact with no
    account cannot be actioned at all. An account created that way is owned by ``owner_user_id``,
    the person who ran the import, exactly as ``import_accounts_csv`` does.
    """
    created = updated = skipped = 0
    errors: list[str] = []
    rows = _rows(content)
    mapped_columns = set(mapping)
    real, custom = split_targets(mapping)

    for index, row in enumerate(rows, start=2):
        fields = {field: (row.get(column) or "").strip() for column, field in real.items()}
        email = normalise_email(fields.get("email", ""))
        full_name = fields.get("full_name", "")

        if not email:
            skipped += 1
            errors.append(f"row {index}: no email address")
            continue

        # The mapped account domain, else the domain of the email itself — which is right far more
        # often than it is wrong for a work address, and leaves the contact actionable either way.
        domain = normalise_domain(fields.get("account_domain", "")) or normalise_domain(
            email.split("@")[-1]
        )
        try:
            # One row, one savepoint — see the account loop above.
            async with ts.session.begin_nested():
                account = await ts.first(Account, Account.domain == domain) if domain else None
                if account is None:
                    account = Account(
                        tenant_id=ts.tenant_id,
                        name=fields.get("account_name") or domain or (full_name or email),
                        source="csv_import",
                        owner_user_id=owner_user_id,
                    )
                    if domain:
                        account.domain = domain
                    ts.add(account)
                    await ts.flush()

                existing = await ts.first(Contact, Contact.email == email)
                if existing is None:
                    contact = Contact(
                        tenant_id=ts.tenant_id,
                        account_id=account.id,
                        full_name=full_name or email.split("@")[0],
                        email=email,
                    )
                    for field in CONTACT_TEXT_FIELDS:
                        value = (fields.get(field) or "").strip()
                        if value:
                            setattr(contact, field, value)
                    extras = _custom_values(row, custom, _extras(row, mapped_columns))
                    if extras:
                        contact.custom_fields = extras
                    ts.add(contact)
                    await ts.flush()
                    created += 1
                else:
                    # Blank cells never overwrite, for the same reason they do not on accounts.
                    if full_name:
                        existing.full_name = full_name
                    for field in CONTACT_TEXT_FIELDS:
                        value = (fields.get(field) or "").strip()
                        if value:
                            setattr(existing, field, value)
                    extras = _custom_values(row, custom, _extras(row, mapped_columns))
                    if extras:
                        existing.custom_fields = {**(existing.custom_fields or {}), **extras}
                    await ts.flush()
                    updated += 1
        except Exception as exc:  # noqa: BLE001 - the database's reason is what the operator needs
            skipped += 1
            errors.append(f"row {index}: {_row_reason(exc)}")

    return {
        "created": created, "updated": updated, "skipped": skipped,
        "total_rows": len(rows), "errors": errors[:50],
    }
