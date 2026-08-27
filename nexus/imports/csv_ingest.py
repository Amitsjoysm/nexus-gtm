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
ACCOUNT_TEXT_FIELDS = ("name", "industry", "country", "region", "postal_code")
ACCOUNT_INT_FIELDS = ("employee_count", "annual_revenue")

# `full_name` and `email` are handled separately: one is the fallback identity, the other IS the
# identity, so neither can be written blindly in a loop.
CONTACT_TEXT_FIELDS = ("title", "seniority", "phone", "linkedin_url")


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


def _rows(content: bytes) -> list[dict]:
    reader = _csv.DictReader(io.StringIO(_decode(content)))
    return [row for _, row in zip(range(MAX_ROWS), reader)]


def _extras(row: dict, mapped_columns: set[str]) -> dict:
    return {
        key.strip(): value.strip()
        for key, value in row.items()
        if key and key not in mapped_columns and isinstance(value, str) and value.strip()
    }


def _to_int(raw: str) -> int | None:
    """Parse '1,200', '$25,000,000' and '25000000'. Returns None for anything else.

    Ops spreadsheets format numbers for humans. Refusing a value with a comma in it would drop the
    revenue column of most real files.
    """
    cleaned = (raw or "").replace(",", "").replace("$", "").replace(" ", "").strip()
    return int(cleaned) if cleaned.isdigit() else None


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
        parsed = _to_int(fields.get(field, ""))
        if parsed is not None:
            setattr(account, field, parsed)

    if extras:
        account.custom_fields = {**(account.custom_fields or {}), **extras}


async def import_accounts_csv(ts, *, content: bytes, mapping: dict[str, str]) -> dict:
    """Create or update accounts from CSV. ``mapping`` is ``{csv_column: account_field}``."""
    created = updated = skipped = 0
    errors: list[str] = []
    rows = _rows(content)
    mapped_columns = set(mapping)

    for index, row in enumerate(rows, start=2):  # 2 == the first data line in a spreadsheet
        fields = {field: (row.get(column) or "").strip() for column, field in mapping.items()}
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

        if existing is None:
            account = Account(tenant_id=ts.tenant_id, name=name or domain, source="csv_import")
            _apply_account(account, fields, _extras(row, mapped_columns))
            ts.add(account)
            created += 1
        else:
            _apply_account(existing, fields, _extras(row, mapped_columns))
            updated += 1
        await ts.flush()

    return {
        "created": created, "updated": updated, "skipped": skipped,
        "total_rows": len(rows), "errors": errors[:50],
    }


async def import_contacts_csv(ts, *, content: bytes, mapping: dict[str, str]) -> dict:
    """Create or update contacts from CSV. Identity is the normalised email within the tenant.

    A contact whose company is not in the book yet **creates** the account. Refusing would make the
    two imports order-dependent for a reason invisible from the upload screen, and a contact with no
    account cannot be actioned at all.
    """
    created = updated = skipped = 0
    errors: list[str] = []
    rows = _rows(content)
    mapped_columns = set(mapping)

    for index, row in enumerate(rows, start=2):
        fields = {field: (row.get(column) or "").strip() for column, field in mapping.items()}
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
        account = await ts.first(Account, Account.domain == domain) if domain else None
        if account is None:
            account = Account(
                tenant_id=ts.tenant_id,
                name=fields.get("account_name") or domain or (full_name or email),
                source="csv_import",
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
            extras = _extras(row, mapped_columns)
            if extras:
                contact.custom_fields = extras
            ts.add(contact)
            created += 1
        else:
            # Blank cells never overwrite, for the same reason they do not on accounts.
            if full_name:
                existing.full_name = full_name
            for field in CONTACT_TEXT_FIELDS:
                value = (fields.get(field) or "").strip()
                if value:
                    setattr(existing, field, value)
            extras = _extras(row, mapped_columns)
            if extras:
                existing.custom_fields = {**(existing.custom_fields or {}), **extras}
            updated += 1
        await ts.flush()

    return {
        "created": created, "updated": updated, "skipped": skipped,
        "total_rows": len(rows), "errors": errors[:50],
    }
