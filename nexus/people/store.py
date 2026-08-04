# nexus/people/store.py
"""Resolve, read and update shared person records.

Everything here takes a **platform** session: ``people`` carries no ``tenant_id``, so a
``TenantSession`` would be the wrong scope and, under RLS, would silently return nothing.

The encryption split, restated because it is the thing that is easy to get wrong later:

* ``*_hash`` columns are ``sha256`` of the normalised value. Indexed, used for every lookup, never
  reversed by us. Fernet is randomised, so the same email seals to a different blob each time and an
  index over ciphertext would match nothing — hashing is what makes the sealed column usable at all.
* ``*_encrypted`` columns are Fernet, decrypted only at the point a human reads the value.

The hash is an index, not anonymisation: email address space is small enough to brute-force. The
encryption is what protects the value if the database is copied.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from nexus.core.crypto import seal_text, unseal_text

logger = logging.getLogger("nexus.people.store")


def _enc_key() -> str:
    """Dedicated key if configured, else derived from ``secret_key`` by ``core.crypto``.

    Separate from the MFA and network-token keys so the three rotate independently: rotating the
    key that protects contact details must not orphan every MFA seed.
    """
    from nexus.core.config import get_settings

    return getattr(get_settings(), "people_data_enc_key", "") or ""


def hash_value(value: str | None) -> str:
    """Stable lookup hash for an already-normalised value. Empty in, empty out."""
    normalised = (value or "").strip().lower()
    if not normalised:
        return ""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def normalise_linkedin(url: str | None) -> str:
    """A comparable LinkedIn profile URL, or "".

    The same profile is shared as ``linkedin.com/in/x``, ``www.linkedin.com/in/x/``,
    ``https://uk.linkedin.com/in/x`` and with tracking parameters attached. Left alone, one person
    resolves to four — which is the duplication this module exists to remove.
    """
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    if raw.startswith("www."):
        raw = raw[4:]
    # Country subdomains (uk., de., ...) address the same profile.
    if ".linkedin.com/" in raw:
        raw = "linkedin.com/" + raw.split(".linkedin.com/", 1)[1]
    raw = raw.rstrip("/")
    if "linkedin.com/in/" not in raw:
        return ""          # a company page or a feed URL is not a person
    return raw


def person_id_for(*, linkedin: str = "", email: str = "") -> str:
    """Deterministic id from the strongest available identity.

    Deterministic so two workers racing to resolve the same person generate the same primary key —
    one insert wins, the other re-reads, instead of both succeeding and splitting one human in two.

    LinkedIn wins over email because a person keeps their profile across jobs while their work
    address changes; keying on email would make every job change look like a new human, which is
    precisely the event ``job_switch`` needs to detect.
    """
    key = normalise_linkedin(linkedin) or (email or "").strip().lower()
    if not key:
        return ""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PersonView:
    """A decrypted person, for the point of use. Never persisted, never logged."""

    id: str
    full_name: str = ""
    title: str = ""
    company_domain: str = ""
    country: str = ""
    linkedin_url: str = ""
    email: str = ""
    phone: str = ""
    phone_raw: str = ""
    phone_status: str = "unattempted"
    last_enriched_at: datetime | None = None


async def resolve_person_record(
    session, *, linkedin_url: str = "", email: str = "", full_name: str = "",
    title: str = "", company_domain: str = "", country: str = "",
):
    """Find or create the shared person for an identity. Returns None when there is no usable one.

    Identity is LinkedIn URL or normalised email and nothing else — see the package docstring for
    why a name plus a company is not enough.

    Fields only ever **fill blanks** on an existing row, exactly like ``companies.resolve_company``.
    One tenant's stale title must not overwrite what every other tenant sees; per-tenant corrections
    stay on ``contacts``.
    """
    from sqlalchemy import select

    from nexus.models.person import Person, PersonIdentity

    linkedin = normalise_linkedin(linkedin_url)
    email_norm = (email or "").strip().lower()
    pid = person_id_for(linkedin=linkedin, email=email_norm)
    if not pid:
        return None

    person = await session.get(Person, pid)
    if person is None and email_norm:
        # A person first seen by LinkedIn URL and now by email (or the reverse) must resolve to the
        # existing row rather than a second one. The identity table is what makes that possible.
        existing = (
            await session.scalars(
                select(PersonIdentity).where(
                    PersonIdentity.kind == "email",
                    PersonIdentity.value_hash == hash_value(email_norm),
                )
            )
        ).first()
        if existing is not None:
            person = await session.get(Person, existing.person_id)

    if person is None:
        person = Person(id=pid, full_name=(full_name or "").strip(), source="contact_backfill")
        session.add(person)

    # Fill blanks only.
    for attr, value in (
        ("full_name", (full_name or "").strip()),
        ("title", (title or "").strip()),
        ("company_domain", (company_domain or "").strip().lower()),
        ("country", (country or "").strip()),
    ):
        if value and not getattr(person, attr, None):
            setattr(person, attr, value)

    if linkedin and not person.linkedin_url:
        person.linkedin_url = linkedin
    if email_norm and not person.email_hash:
        person.email_hash = hash_value(email_norm)
        person.email_encrypted = seal_text(email_norm, key=_enc_key())

    await session.flush()
    await _ensure_identity(session, person.id, "linkedin", linkedin)
    await _ensure_identity(session, person.id, "email", email_norm)
    return person


async def _ensure_identity(session, person_id: str, kind: str, value: str) -> None:
    """Attach an alternate key, ignoring one that already belongs to this person.

    A unique constraint covers the race; a conflict that points at a *different* person is logged
    rather than merged. Merging two people on a shared address is exactly the wrong-attribution
    failure this subsystem is built to avoid, and it needs a human.
    """
    from sqlalchemy import select

    from nexus.models.person import PersonIdentity

    if not value:
        return
    digest = hash_value(value)
    existing = (
        await session.scalars(
            select(PersonIdentity).where(
                PersonIdentity.kind == kind, PersonIdentity.value_hash == digest
            )
        )
    ).first()
    if existing is not None:
        if existing.person_id != person_id:
            logger.warning(
                "person identity %s/%s already belongs to %s, not %s — not merging",
                kind, digest[:8], existing.person_id, person_id,
            )
        return
    session.add(PersonIdentity(person_id=person_id, kind=kind, value_hash=digest))
    await session.flush()


async def read_person(session, person_id: str) -> PersonView | None:
    """Decrypt a person for display. The only place sealed values are opened."""
    from nexus.models.person import Person

    person = await session.get(Person, person_id)
    if person is None:
        return None
    key = _enc_key()
    return PersonView(
        id=person.id,
        full_name=person.full_name or "",
        title=person.title or "",
        company_domain=person.company_domain or "",
        country=person.country or "",
        linkedin_url=person.linkedin_url or "",
        email=unseal_text(person.email_encrypted or "", key=key),
        phone=unseal_text(person.phone_encrypted or "", key=key),
        phone_raw=unseal_text(person.phone_raw_encrypted or "", key=key),
        phone_status=person.phone_status,
        last_enriched_at=person.last_enriched_at,
    )


async def record_phone_lookup(
    session, person_id: str, *, phone: str = "", raw: str = "", source: str = "apify",
    status: str = "",
) -> bool:
    """Store the outcome of a paid phone lookup. Returns True when a number was stored.

    ``not_found`` is recorded deliberately. A miss is an expensive answer, and without persisting it
    the same empty lookup is re-purchased on every crawl — the difference between a bounded monthly
    bill and an unbounded one. ``read_person`` and the enrichment caller both treat a recorded miss
    as "already asked".
    """
    from nexus.models.person import Person

    person = await session.get(Person, person_id)
    if person is None:
        return False

    key = _enc_key()
    person.last_enriched_at = datetime.now(timezone.utc)
    person.enrichment_source = source

    if phone:
        person.phone_hash = hash_value(phone)
        person.phone_encrypted = seal_text(phone, key=key)
        person.phone_status = status or "found"
    elif raw:
        # Unparseable, but still the only number we have for this person.
        person.phone_raw_encrypted = seal_text(raw, key=key)
        person.phone_status = status or "found"
    else:
        person.phone_status = status or "not_found"

    await session.flush()
    return bool(phone or raw)


async def forget_person(session, person_id: str) -> bool:
    """Erase a person: the shared row and every identity pointing at it.

    A shared store makes this **easier**, not harder. Per-tenant contacts mean an erasure request is
    N deletes across N workspaces with no list of which ones hold the person; here it is one row,
    and the cascade takes the identities with it. Per-tenant ``contacts`` rows are the tenant's own
    record of their own relationship and are handled by the tenant-facing delete.
    """
    from sqlalchemy import delete

    from nexus.models.person import Person, PersonIdentity

    person = await session.get(Person, person_id)
    if person is None:
        return False

    # Delete the identities explicitly rather than leaning on ``ondelete="CASCADE"``. The FK
    # cascade is enforced by Postgres but NOT by SQLite unless `PRAGMA foreign_keys=ON`, so relying
    # on it means erasure works in production and silently leaves orphaned identity hashes in the
    # test suite — the same "passes every test, differs in prod" trap CLAUDE.md documents for RLS.
    # An erasure that half-completes is the one operation where that is unacceptable.
    await session.execute(delete(PersonIdentity).where(PersonIdentity.person_id == person_id))
    await session.delete(person)
    await session.flush()
    logger.info("erased shared person record %s", person_id)
    return True
