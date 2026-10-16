"""Who a row is, and the two keys derived from that.

The provider returns no record id, so identity is derived with the rules the shared stores use
(``nexus/people``, ``nexus/companies``): a person is their normalised email, else first and last
name at the company's normalised domain; a company is its normalised domain, else its normalised
name. A person with neither an email nor a usable company domain has no identity and cannot be
bought: a name alone is not a person.

* ``fingerprint`` is sha256 of the identity. It stays on the server (the paid ledger, the cache).
* ``row_key`` is what the browser holds to point at a row. It is an HMAC bound to the tenant under a
  key derived from ``secret_key``, never ``sha256(email)``: the email space is small enough to
  brute-force (``nexus/people`` says the same of its own hash column), so a bare hash in the DOM
  would be the unmasked address one dictionary away. Bound to the tenant, a key copied from one
  workspace means nothing in another.
"""
from __future__ import annotations

import hashlib
import hmac

from nexus.accounts.dedupe import normalise_name
from nexus.companies.resolution import normalise_domain
from nexus.prospector.records import VendorCompany, VendorContact


def domain_of(url: str) -> str:
    return normalise_domain(url or "")


def _email(value: str) -> str:
    value = (value or "").strip().lower()
    return value if "@" in value and "." in value.rsplit("@", 1)[-1] else ""


def contact_identity(record: VendorContact) -> str:
    email = _email(record.email)
    if email:
        return f"e:{email}"
    name = " ".join(record.full_name.lower().split())
    domain = domain_of(record.company_url)
    if name and domain:
        return f"n:{name}|{domain}"
    return ""


def company_identity(record: VendorCompany) -> str:
    domain = domain_of(record.company_url)
    if domain:
        return f"d:{domain}"
    name = normalise_name(record.company_name)
    return f"n:{name}" if name else ""


def fingerprint(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _row_secret() -> bytes:
    from nexus.core.config import get_settings

    return hashlib.sha256(f"prospector-row-key|{get_settings().secret_key}".encode()).digest()


def row_key(tenant_id: str, identity: str) -> str:
    message = f"{tenant_id}|{identity}".encode("utf-8")
    return hmac.new(_row_secret(), message, hashlib.sha256).hexdigest()[:32]
