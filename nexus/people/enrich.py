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


_PHONE_KEYS = ("phone", "phone_number", "phoneNumber", "mobile", "mobile_number", "telephone")
_PHONE_LIST_KEYS = ("phoneNumbers", "phones", "contact_numbers")
# Where an actor puts the profile the row is about. Several spellings, same reason as the phone
# keys: actor output is not a contract.
_PROFILE_KEYS = (
    "linkedin_url", "linkedinUrl", "linkedinProfileUrl", "profile_url", "profileUrl",
    "linkedin", "profile", "url", "inputUrl", "input_url",
)


def _first_phone_in(item: dict) -> str:
    """The first value in this one row that actually looks like a phone number."""
    from nexus.contacts.phone import looks_like_phone

    def _take(value) -> str:
        if isinstance(value, str) and looks_like_phone(value):
            return value.strip()
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and looks_like_phone(entry):
                    return entry.strip()
                if isinstance(entry, dict):
                    for nested in _PHONE_KEYS:
                        got = entry.get(nested)
                        if isinstance(got, str) and looks_like_phone(got):
                            return got.strip()
        return ""

    for key in (*_PHONE_KEYS, *_PHONE_LIST_KEYS):
        found = _take(item.get(key))
        if found:
            return found
    nested_contact = item.get("contact")
    if isinstance(nested_contact, dict):
        return _first_phone_in(nested_contact)
    return ""


def _row_is_about(item: dict, expect: str) -> bool:
    """Whether this dataset row is about the profile we asked for.

    ``expect`` is an already-normalised LinkedIn URL. Compared against every spelling of the
    profile field an actor might use, each normalised the same way, so
    ``https://www.linkedin.com/in/x/`` and ``linkedin.com/in/x`` match.
    """
    from nexus.people.store import normalise_linkedin

    for key in _PROFILE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and normalise_linkedin(value) == expect:
            return True
        # Some actors nest the echoed input one level down.
        if isinstance(value, dict):
            for nested in _PROFILE_KEYS:
                got = value.get(nested)
                if isinstance(got, str) and normalise_linkedin(got) == expect:
                    return True
    return False


def extract_phone(items: list[dict], *, expect_linkedin_url: str = "") -> str:
    """Pull a usable phone number out of an actor's dataset.

    Two things this must get right, and it got neither before:

    **1. The row has to be about the person we asked for.** The actor is called with a LIST of
    profile URLs and returns a dataset; taking ``items[0]``'s phone means that whenever it returns
    more than one row — a partial match, a suggested profile, a stale row from a batched run — we
    attach a stranger's number to a contact. That is the same wrong-attribution class as the six
    bugs in `nexus/companies/`, and it is worse here: a rep phones a real person with someone
    else's context. When ``expect_linkedin_url`` is given, rows that identify a *different*
    profile are skipped outright. Rows that identify **no** profile are used only as a fallback,
    because a single-result actor that echoes nothing back is the common, benign case — but a row
    that names someone else is never a fallback.

    **2. The value has to look like a phone number.** Actor output is not a contract, so this
    reads six key spellings plus nested and list shapes — but every candidate now passes
    ``looks_like_phone`` first. Without it, ``{"phone": "Premium feature"}`` was returned as a
    phone number, recorded as a successful lookup, and cached in the shared person record where it
    suppressed re-lookup for every tenant until the TTL expired.
    """
    from nexus.people.store import normalise_linkedin

    expect = normalise_linkedin(expect_linkedin_url) if expect_linkedin_url else ""
    rows = [i for i in items if isinstance(i, dict)]

    if expect:
        matched = [i for i in rows if _row_is_about(i, expect)]
        if matched:
            for item in matched:
                found = _first_phone_in(item)
                if found:
                    return found
            return ""
        # Nothing identified itself as our profile. Fall back only to rows that name no profile at
        # all; a row naming a different person is discarded rather than used.
        rows = [i for i in rows if not _identifies_a_profile(i)]

    for item in rows:
        found = _first_phone_in(item)
        if found:
            return found
    return ""


def _identifies_a_profile(item: dict) -> bool:
    """Whether the row names any LinkedIn profile at all (whoever it belongs to)."""
    from nexus.people.store import normalise_linkedin

    for key in _PROFILE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and normalise_linkedin(value):
            return True
    return False


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

    # Pass the profile we asked about, so a dataset row belonging to somebody else can never
    # supply the number. See extract_phone.
    found = extract_phone(items, expect_linkedin_url=linkedin_url)
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
