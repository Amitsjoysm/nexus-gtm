# nexus/people/enrich.py
"""Paid contact enrichment, bought once and shared.

A phone lookup is an Apify actor run — real money per call. Per-tenant, forty workspaces tracking
one VP Engineering buy that number forty times. This module is the seam where the shared person
record turns that into one purchase.

**Metering and cost are separate questions, deliberately.** The capability is metered on every
call, cache hit or not: the customer received a phone number and is charged for the number, not for
our infrastructure. What the shared store improves is **COGS**, which is captured only when an
actor actually runs. Charging only on a miss would hand the saving to whichever customer happened
to ask second, which is arbitrary, and would make revenue depend on crawl ordering.

**A recorded miss is not re-purchased.** ``phone_status == "not_found"`` means we already paid to
learn there is nothing there. Re-asking on every crawl is the difference between a bounded monthly
bill and an unbounded one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("nexus.people.enrich")

# The capability the billing seam charges. A phone lookup is priced separately from generic contact
# enrichment because its COGS is an actor run rather than an API call.
PHONE_CAPABILITY = "enrich.phone"

# How long a recorded answer is trusted before it is worth paying again. People change numbers, but
# not often; re-buying a known answer weekly would erase the whole saving.
DEFAULT_TTL_DAYS = 180


@dataclass(slots=True)
class PhoneResult:
    phone: str = ""
    raw: str = ""
    source: str = ""
    cached: bool = False
    status: str = "not_found"

    @property
    def ok(self) -> bool:
        return bool(self.phone or self.raw)


def _fresh(last_enriched_at: datetime | None, ttl_days: int) -> bool:
    if last_enriched_at is None:
        return False
    stamped = last_enriched_at
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamped < timedelta(days=ttl_days)


def extract_phone(items: list[dict]) -> str:
    """Pull the first usable phone number out of an actor's dataset.

    Actors are third-party code and their output shape is not a contract: the same actor has been
    seen returning ``phone``, ``phone_number``, ``phoneNumbers`` (a list) and a nested ``contact``
    object. Reading one hard-coded key would make an upstream rename look like "this person has no
    phone number", which is silent and indistinguishable from the truth.
    """
    keys = ("phone", "phone_number", "phoneNumber", "mobile", "mobile_number", "telephone")
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
                if isinstance(first, dict):
                    for nested in keys:
                        if isinstance(first.get(nested), str) and first[nested].strip():
                            return first[nested].strip()
        for plural in ("phoneNumbers", "phones", "contact_numbers"):
            value = item.get(plural)
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
                if isinstance(first, dict):
                    for nested in keys:
                        if isinstance(first.get(nested), str) and first[nested].strip():
                            return first[nested].strip()
        nested_contact = item.get("contact")
        if isinstance(nested_contact, dict):
            found = extract_phone([nested_contact])
            if found:
                return found
    return ""


async def find_phone(
    ts, *, linkedin_url: str, email: str = "", full_name: str = "", country: str = "",
    account_country: str = "", ttl_days: int = DEFAULT_TTL_DAYS, user_id: str | None = None,
) -> PhoneResult:
    """A phone number for this person, from the shared record when we already bought it.

    ``ts`` is the requesting tenant's session — used only for metering, never for reading the
    shared record, which is platform-global.

    **Ordering matters.** Every platform write completes and commits *before* the tenant session is
    touched for metering. Doing it the other way round nests a second connection's write inside an
    open tenant transaction, which Postgres tolerates and SQLite deadlocks on — a "works in
    production, hangs the test suite" split that is not worth the elegance.

    Never raises. An enrichment failure returns an empty result: losing the contact you were about
    to call because a scraper was down is a worse outcome than a blank field.
    """
    from nexus.core.db import get_platform_sessionmaker
    from nexus.people.store import read_person, record_phone_lookup, resolve_person_record

    try:
        # 1. Resolve and read the shared record. Platform connection only.
        async with get_platform_sessionmaker()() as session:
            person = await resolve_person_record(
                session, linkedin_url=linkedin_url, email=email, full_name=full_name,
                country=country,
            )
            if person is None:
                # No LinkedIn URL and no email: no shared identity, so nothing to look up and
                # nothing to cache against. Deliberate — see the package docstring.
                return PhoneResult(status="no_identity")
            person_id = person.id
            stored_linkedin = person.linkedin_url or linkedin_url
            await session.commit()
            view = await read_person(session, person_id)

        if view is not None and _fresh(view.last_enriched_at, ttl_days):
            # Already bought, including a recorded miss — no actor run. Still METERED: the customer
            # received an answer and is charged for the answer, not for our infrastructure. What the
            # shared store improves is COGS, not price. Billing only on a miss would hand the saving
            # to whichever customer happened to ask second, which is arbitrary, and would make
            # revenue depend on crawl ordering.
            await _meter_lookup(ts, user_id=user_id, cached=True)
            return PhoneResult(
                phone=view.phone, raw=view.phone_raw, source="shared_person",
                cached=True, status=view.phone_status,
            )

        # 2. Buy it. No database transaction is open across the actor run.
        result = await _run_phone_actor(
            linkedin_url=stored_linkedin, country=country, account_country=account_country,
        )

        # 3. Persist the outcome, still on the platform connection, and commit.
        if result.status not in ("unconfigured", "no_identity"):
            async with get_platform_sessionmaker()() as session:
                await record_phone_lookup(
                    session, person_id, phone=result.phone, raw=result.raw, source="apify",
                )
                await session.commit()

        # 4. Only now touch the tenant session. Charged for the lookup, not the result — a
        #    `not_found` is an answer we paid an actor to obtain. Not charged when the integration
        #    was never configured, because nothing was bought.
        if result.status in ("found", "not_found"):
            await _meter_lookup(ts, user_id=user_id, cached=False)
        return result
    except Exception:
        logger.warning("phone enrichment failed for %s", linkedin_url, exc_info=True)
        return PhoneResult(status="failed")


async def _meter_lookup(ts, *, user_id: str | None, cached: bool = False) -> None:
    """Charge the tenant for one phone lookup.

    Metering never blocks the result: the customer already has the number, and a billing write that
    fails must not turn a successful lookup into an error. The engine's own bias is the same —
    unknown capability allows, engine error allows.
    """
    from nexus.billing.meter import metered

    try:
        async with metered(
            ts, PHONE_CAPABILITY, user_id=user_id, source="enrichment",
            # `cached` is what makes the margin visible: same revenue, no COGS. Without the flag,
            # the shared store's saving is invisible in the usage stream.
            attrs={"provider": "apify", "cached": cached},
        ):
            pass
    except Exception:
        logger.warning("metering %s failed; the lookup still stands", PHONE_CAPABILITY,
                       exc_info=True)


async def _run_phone_actor(
    *, linkedin_url: str, country: str, account_country: str,
) -> PhoneResult:
    """Call the actor and canonicalise. Returns an empty result rather than raising."""
    from nexus.contacts.phone import normalise_phone
    from nexus.integrations.apify import ApifyNotConfigured, get_apify_client

    if not linkedin_url:
        return PhoneResult(status="no_identity")

    client = get_apify_client()
    if not client.configured:
        # Inert until keyed, and it says so. A missing key must never read as "no phone number".
        logger.info("phone lookup skipped: Apify is not configured")
        return PhoneResult(status="unconfigured")

    try:
        items = await client.run_actor("phone_finder", {"linkedin_url": [linkedin_url]})
    except ApifyNotConfigured:
        return PhoneResult(status="unconfigured")

    found = extract_phone(items)
    if not found:
        return PhoneResult(status="not_found")

    normalised = normalise_phone(found, country=country, account_country=account_country)
    return PhoneResult(
        phone=normalised.e164,
        # Only keep the raw string when it could not be canonicalised — otherwise every row would
        # carry the same number twice.
        raw="" if normalised.ok else normalised.raw,
        source="apify",
        status="found",
    )
