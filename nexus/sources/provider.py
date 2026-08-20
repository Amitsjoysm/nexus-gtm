# nexus/sources/provider.py
"""Step 7: reading a verified source database as an enrichment provider.

This is the half of the subsystem that finally saves money. Steps 1–6 prove a source is safe to
read; this module reads it, **ahead of the paid APIs**, and lands what it finds in the shared
``companies`` / ``people`` stores so the answer is bought once for every tenant rather than once
per tenant.

Three rules, and every function here is shaped by them.

**1. Verified AND enabled, never one of the two.** ``service.usable_sources()`` is the single
predicate, and ``SourceDatabase.is_usable()`` is what it encodes. A provider that checked
``status`` alone would keep reading a source an operator switched off mid-incident, which is the
one moment the kill switch exists for.

**2. It never raises, and it never stops collection.** The locked failure posture from the plan:
"fall through to the paid provider, never stop collection. It is an optimisation, not a
dependency." So a source that is unreachable, slow, or mapped onto the wrong column returns *no
answer* — not an error — and the caller proceeds to the paid path exactly as if this module were
not installed. Every entry point is total: it returns ``None`` or an empty result, never an
exception. That is also why each source is tried independently; one broken source must not mask a
working one behind it.

**3. A row is used only if it is provably about who we asked for.** The source database is
somebody else's data and we do not control its normalisation, so ``WHERE domain IN (...)`` is a
*candidate* filter, not proof. Every returned row is re-checked in Python against the identity we
asked for, through the same normalisers the shared stores key on. This is not defensive
programming for its own sake: wrong attribution is the failure mode this subsystem has shipped six
times, and here it would write one company's firmographics — or one human's phone number — into a
store that every tenant reads.

**Metering is deliberately NOT a saving for the customer.** A source-database hit is metered
identically to a paid-provider hit, carrying ``attrs.cached`` exactly as ``nexus/people/enrich.py``
does. The customer is charged for the answer, not for our infrastructure; what a registered source
improves is **COGS**, and the flag is what makes that margin visible in the usage stream. Billing
only on a miss would hand the saving to whichever customer happened to ask second, which is
arbitrary, and would make revenue depend on crawl ordering.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.sources.provider")

# What a hit is attributed to, in `companies.source` / `people.source` and in the usage stream.
# `COMPANY_SOURCES` already carries it — the schema was written expecting this module.
SOURCE_NAME = "source_db"


@dataclass(slots=True)
class SourceHit:
    """One row from one registered source, already proved to be about the identity we asked for."""

    entity: str
    fields: dict = field(default_factory=dict)
    source_id: str = ""
    source_name: str = ""

    def get(self, key: str) -> str:
        value = self.fields.get(key)
        return "" if value is None else str(value).strip()

    def employee_count(self) -> int | None:
        """The headcount this row carries, or None. Never raises.

        A source's column may be an integer, a numeric string, or a band like ``"51-200"`` that
        does not reduce to one number. Anything that is not a plain positive count is dropped
        rather than guessed at: headcount drives ICP scoring, and a wrong number there silently
        moves accounts in and out of a rep's list.
        """
        raw = self.fields.get("employee_count")
        if isinstance(raw, bool):  # bool is an int subclass; True would become 1
            return None
        try:
            parsed = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None


# ---- choosing sources -------------------------------------------------------------------------
async def _candidates(entity: str, key_field: str) -> list:
    """Usable sources that can answer this kind of lookup. Returns [] rather than raising.

    Filtering on the mapped key here (rather than letting ``build_lookup`` reject it later) keeps
    a source mapped onto ``email`` from being counted as a failure when the caller only has a
    LinkedIn URL. It has nothing to say about that identity; that is not the same as being broken,
    and logging it as one would bury the sources that really are.
    """
    from nexus.sources import service

    try:
        sources = await service.usable_sources()
    except Exception:
        # Our own database, not the foreign one — but the posture is the same. If we cannot even
        # read the registry, enrichment falls through to the paid provider rather than failing.
        logger.warning("could not list usable source databases", exc_info=True)
        return []

    usable = []
    for row in sources:
        mapping = row.mapping or {}
        if mapping.get("entity") != entity:
            continue
        if not (mapping.get("columns") or {}).get(key_field):
            continue
        # Belt and braces: `usable_sources` already filters on this, and it stays true here so a
        # future caller that assembles its own list cannot skip the gate.
        if not row.is_usable():
            continue
        usable.append(row)
    return usable


# ---- identity, and proving a row is about it ---------------------------------------------------
def _domain_candidates(domain: str) -> list[str]:
    """Spellings of one domain a source might plausibly store.

    We normalise to ``stripe.com``; a source database may hold ``www.stripe.com`` or
    ``https://stripe.com/``. Rather than apply a function to the column — which would defeat
    whatever index the source has on a table we do not own — we ask for the handful of spellings
    that mean the same thing and then verify what comes back. The list is generated by us and is
    bounded; it never grows with anything the source returns.
    """
    if not domain:
        return []
    return [
        domain,
        f"www.{domain}",
        f"https://{domain}",
        f"https://www.{domain}",
        f"http://{domain}",
    ]


def _linkedin_candidates(url: str) -> list[str]:
    """Spellings of one normalised LinkedIn profile URL. Same reasoning as ``_domain_candidates``."""
    if not url:
        return []
    # `url` arrives already normalised to `linkedin.com/in/x`.
    return [url, f"https://{url}", f"https://www.{url}", f"www.{url}", f"{url}/", f"https://{url}/"]


def _row_is_about_company(row: dict, *, domain: str) -> bool:
    """Whether this row really is the company we asked for, by the shared store's own key."""
    from nexus.companies.resolution import normalise_domain

    return normalise_domain(str(row.get("domain") or "")) == domain


def _row_is_about_person(row: dict, *, linkedin: str, email: str) -> bool:
    """Whether this row really is the person we asked for.

    Checked against the identity we *queried on*, and only that one. A row matching on the other
    key is not accepted as a bonus: if we asked by LinkedIn URL and the row's URL disagrees, the
    source has handed us a different human, whatever its email column says.
    """
    from nexus.people.store import normalise_linkedin

    if linkedin:
        return normalise_linkedin(str(row.get("linkedin_url") or "")) == linkedin
    if email:
        return str(row.get("email") or "").strip().lower() == email
    return False


# ---- the lookups ------------------------------------------------------------------------------
async def _first_match(entity: str, key_field: str, keys: list[str], matches) -> SourceHit | None:
    """Ask each usable source in turn for ``keys``; return the first row that passes ``matches``.

    Sources are tried independently and a failing one is skipped, not fatal: with several
    registered, one unreachable warehouse must not hide the answer sitting in the next.
    """
    if not keys:
        return None
    from nexus.sources import engine

    for row in await _candidates(entity, key_field):
        try:
            found = await engine.fetch_by_identity(
                row.dsn_encrypted, row.mapping or {}, key_field=key_field, key_values=keys
            )
        except Exception as exc:
            # Unreachable, slow, re-pointed at a dropped table, mapping gone stale — all the same
            # from here. Logged at warning because a source that has quietly stopped answering is
            # worth an operator's attention, and dropped because collection must continue.
            logger.warning("source database %s did not answer: %r", row.name, exc)
            continue
        for candidate in found:
            if matches(candidate):
                return SourceHit(
                    entity=entity, fields=dict(candidate),
                    source_id=row.id, source_name=row.name,
                )
        if found:
            # Rows came back and none of them were about the identity we asked for. That is the
            # wrong-attribution signature, and it is worth saying out loud: the alternative is a
            # source that looks healthy while its key column means something else.
            logger.info(
                "source database %s returned %d row(s), none matching the %s asked for",
                row.name, len(found), key_field,
            )
    return None


async def lookup_company(domain: str) -> SourceHit | None:
    """Firmographics for one domain from a registered source, or ``None``. Never raises."""
    from nexus.companies.resolution import normalise_domain

    normalised = normalise_domain(domain)
    if not normalised:
        # No usable domain means no identity, exactly as in `companies.resolution`. Nothing to key
        # on and nothing to cache against.
        return None
    try:
        return await _first_match(
            "company", "domain", _domain_candidates(normalised),
            lambda row: _row_is_about_company(row, domain=normalised),
        )
    except Exception:
        logger.warning("company source lookup failed for %r", normalised, exc_info=True)
        return None


async def lookup_person(*, linkedin_url: str = "", email: str = "") -> SourceHit | None:
    """One person from a registered source, keyed on LinkedIn URL or email. Never raises.

    LinkedIn wins when both are given, for the reason ``people.store.person_id_for`` gives: a
    profile survives a job change and a work address does not.
    """
    from nexus.people.store import normalise_linkedin

    linkedin = normalise_linkedin(linkedin_url)
    email_norm = (email or "").strip().lower()
    try:
        if linkedin:
            hit = await _first_match(
                "person", "linkedin_url", _linkedin_candidates(linkedin),
                lambda row: _row_is_about_person(row, linkedin=linkedin, email=""),
            )
            if hit is not None:
                return hit
        if email_norm:
            return await _first_match(
                "person", "email", [email_norm],
                lambda row: _row_is_about_person(row, linkedin="", email=email_norm),
            )
    except Exception:
        logger.warning("person source lookup failed", exc_info=True)
    return None


# ---- landing results in the shared stores -------------------------------------------------------
async def enrich_company(domain: str) -> SourceHit | None:
    """Look a company up and write it into the shared ``companies`` store. Never raises.

    Platform session throughout: ``companies`` carries no ``tenant_id``, so a ``TenantSession``
    would be the wrong scope and, under RLS, would silently see nothing.

    ``resolve_company`` fills blanks only, so a source database can complete a record but never
    overwrite one — the same rule that keeps one tenant's correction from rewriting everybody's
    data applies to a source we registered on their behalf.
    """
    hit = await lookup_company(domain)
    if hit is None:
        return None
    try:
        from nexus.core.db import get_platform_sessionmaker
        from nexus.companies.resolution import resolve_company

        employees = hit.employee_count()
        async with get_platform_sessionmaker()() as session:
            await resolve_company(
                session,
                domain=hit.get("domain"),
                name=hit.get("name"),
                industry=hit.get("industry") or None,
                country=hit.get("country") or None,
                employee_count=employees,
                source=SOURCE_NAME,
            )
            await session.commit()
    except Exception:
        # The shared write failing must not discard an answer the caller can still use: the hit is
        # correct, we simply did not manage to share it. Returning it keeps this call useful and
        # leaves the next one to retry the write.
        logger.warning("could not land source company %r in the shared store", domain,
                       exc_info=True)
    return hit


def source_phone(hit: SourceHit, *, country: str = "", account_country: str = "") -> str:
    """The E.164 phone this hit carries, or "". Never raises.

    Two gates, both of which the paid Apify path also applies, for the same reasons:
    ``looks_like_phone`` because a column named ``phone`` holding "Premium feature" is not a
    number and recording it would suppress re-lookup for every tenant until the TTL expired; and
    ``normalise_phone`` because a source-database number must land in the shared store in the same
    shape an actor's would, or the same person reads differently depending on where we got them.
    """
    from nexus.contacts.phone import looks_like_phone, normalise_phone

    raw = hit.get("phone")
    if not raw:
        return ""
    if not looks_like_phone(raw):
        logger.info(
            "source database %s returned a non-phone in its phone column", hit.source_name
        )
        return ""
    normalised = normalise_phone(raw, country=country, account_country=account_country)
    return normalised.e164 or raw


async def enrich_person(
    *, linkedin_url: str = "", email: str = "",
    country: str = "", account_country: str = "",
) -> SourceHit | None:
    """Look a person up and write them into the shared ``people`` store. Never raises.

    A phone number found here is recorded through ``record_phone_lookup`` — the same write the
    paid Apify path uses — so the next tenant to ask reads it from the shared record and neither
    pays an actor nor re-reads the source.
    """
    hit = await lookup_person(linkedin_url=linkedin_url, email=email)
    if hit is None:
        return None
    try:
        from nexus.core.db import get_platform_sessionmaker
        from nexus.people.store import record_phone_lookup, resolve_person_record

        from nexus.models.person import Person
        from nexus.people.store import person_id_for

        phone = source_phone(hit, country=country, account_country=account_country)
        identity_linkedin = hit.get("linkedin_url") or linkedin_url
        identity_email = hit.get("email") or email

        async with get_platform_sessionmaker()() as session:
            # `source` records how a person came to EXIST, so it is only ours to set when this
            # lookup is what created them. Stamping it unconditionally would relabel a person who
            # arrived by contact backfill months ago, quietly rewriting provenance that exists to
            # settle disagreements between sources.
            pid = person_id_for(linkedin=identity_linkedin, email=identity_email)
            existed = bool(pid) and (await session.get(Person, pid)) is not None

            person = await resolve_person_record(
                session,
                linkedin_url=identity_linkedin,
                email=identity_email,
                full_name=hit.get("full_name"),
                title=hit.get("title"),
                company_domain=hit.get("company_domain"),
                country=country,
            )
            if person is not None:
                # `person.id != pid` means the resolver matched an existing human through the
                # identity table rather than minting the one we keyed on — also not ours to label.
                if not existed and person.id == pid:
                    person.source = SOURCE_NAME
                if phone:
                    # `enrichment_source` is the field that says who supplied the DATA, and
                    # `record_phone_lookup` sets it on every call — so a source-database number is
                    # attributable even on a person we did not create.
                    await record_phone_lookup(
                        session, person.id, phone=phone, source=SOURCE_NAME,
                    )
            await session.commit()
    except Exception:
        logger.warning("could not land source person in the shared store", exc_info=True)
    return hit


# ---- metering ------------------------------------------------------------------------------
async def meter_hit(ts, capability: str, *, user_id: str | None = None,
                    source: str = "enrichment", source_name: str = "") -> None:
    """Charge the tenant for an answer that came from a registered source.

    Metered **identically** to the paid provider it replaced, and for the same reason
    ``nexus/people/enrich.py`` meters a shared-store hit: the customer received the answer and is
    charged for the answer, not for our infrastructure. ``cached=True`` is what keeps the margin
    visible — same revenue, no COGS — and without it the whole point of registering a source is
    invisible in the usage stream.

    Never blocks the result. The customer already has the answer; a billing write that fails must
    not turn a successful lookup into an error.
    """
    from nexus.billing.meter import metered

    try:
        async with metered(
            ts, capability, user_id=user_id, source=source,
            attrs={"provider": SOURCE_NAME, "cached": True, "source_db": source_name},
        ):
            pass
    except Exception:
        logger.warning("metering %s failed; the lookup still stands", capability, exc_info=True)
