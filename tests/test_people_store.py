# tests/test_people_store.py
"""The shared people store: resolve once, enrich once, encrypted at rest.

Two properties carry the whole design and each is asserted here rather than trusted:

* **Identity is explicit.** LinkedIn URL or normalised email, never a name match. Getting a *person*
  wrong means a rep phones a stranger with someone else's context, which is a worse failure than
  the six wrong-company bugs this codebase has already shipped.
* **Values are sealed, hashes are indexed.** The plaintext email must not be readable from the
  database, and the lookup must still work without decrypting anything.
"""
from __future__ import annotations

from nexus.core.db import get_platform_sessionmaker
from nexus.people import (
    forget_person,
    hash_value,
    person_id_for,
    read_person,
    record_phone_lookup,
    resolve_person_record,
)
from nexus.people.store import normalise_linkedin

EMAIL = "dana.scully@acme.com"
LINKEDIN = "https://www.linkedin.com/in/dana-scully/"


# ---- identity -----------------------------------------------------------------------------------

def test_linkedin_urls_for_one_profile_all_normalise_together():
    """Left alone, the same profile shared four ways becomes four people."""
    forms = [
        "https://www.linkedin.com/in/dana-scully/",
        "http://linkedin.com/in/dana-scully",
        "https://uk.linkedin.com/in/dana-scully/",
        "www.linkedin.com/in/dana-scully?utm_source=x",
    ]
    assert len({normalise_linkedin(f) for f in forms}) == 1


def test_a_company_page_is_not_a_person():
    assert normalise_linkedin("https://linkedin.com/company/acme") == ""
    assert normalise_linkedin("https://linkedin.com/feed/") == ""


def test_linkedin_wins_over_email_as_the_identity_key():
    """A person keeps their profile across jobs while their work address changes. Keying on email
    would make every job change look like a new human — the exact event job_switch must detect."""
    by_both = person_id_for(linkedin=LINKEDIN, email=EMAIL)
    by_linkedin = person_id_for(linkedin=LINKEDIN)
    assert by_both == by_linkedin


def test_no_usable_identity_yields_no_person():
    """A contact with neither a profile nor an email stays entirely per-tenant. Safety property,
    not a gap: a name plus a company is not an identity."""
    assert person_id_for() == ""
    assert person_id_for(linkedin="", email="") == ""


async def test_resolving_twice_returns_one_person():
    async with get_platform_sessionmaker()() as s:
        a = await resolve_person_record(s, linkedin_url=LINKEDIN, full_name="Dana Scully")
        b = await resolve_person_record(s, linkedin_url=LINKEDIN, full_name="D. Scully")
        await s.commit()
    assert a.id == b.id


async def test_a_person_seen_first_by_email_then_by_linkedin_stays_one_row():
    """The identity table exists for exactly this: two tenants that know different things about
    the same human must not create two records."""
    from sqlalchemy import func, select

    from nexus.models.person import Person

    async with get_platform_sessionmaker()() as s:
        await resolve_person_record(s, email="mulder@acme.com", full_name="Fox Mulder")
        await s.commit()
    async with get_platform_sessionmaker()() as s:
        again = await resolve_person_record(
            s, email="mulder@acme.com", linkedin_url="https://linkedin.com/in/fox-mulder/",
        )
        await s.commit()
        count = await s.scalar(select(func.count()).select_from(Person))
    assert again is not None
    assert count == 1, "resolving by a second identity must not fork the person"


async def test_fields_only_ever_fill_blanks():
    """One tenant's stale title must not rewrite what every other tenant sees."""
    async with get_platform_sessionmaker()() as s:
        await resolve_person_record(s, linkedin_url=LINKEDIN, title="VP Engineering")
        await s.commit()
    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, linkedin_url=LINKEDIN, title="Intern")
        await s.commit()
    assert person.title == "VP Engineering"


# ---- encryption ----------------------------------------------------------------------------------

async def test_the_email_is_not_readable_from_the_row():
    """The point of sealing: a database copy does not hand over contact details."""
    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, email=EMAIL, full_name="Dana Scully")
        await s.commit()
        stored = person.email_encrypted

    assert stored
    assert EMAIL not in stored
    assert "acme.com" not in stored


async def test_the_hash_finds_the_person_without_decrypting():
    """Fernet is randomised, so an index over ciphertext would match nothing. The hash column is
    what makes the sealed column usable at all."""
    from sqlalchemy import select

    from nexus.models.person import Person

    async with get_platform_sessionmaker()() as s:
        await resolve_person_record(s, email=EMAIL)
        await s.commit()
    async with get_platform_sessionmaker()() as s:
        found = (
            await s.scalars(select(Person).where(Person.email_hash == hash_value(EMAIL)))
        ).first()
    assert found is not None


async def test_sealing_the_same_value_twice_produces_different_ciphertext():
    """Which is why the hash exists — asserted so nobody 'optimises' the hash column away."""
    from nexus.core.crypto import seal_text

    assert seal_text(EMAIL) != seal_text(EMAIL)


async def test_reading_a_person_decrypts_email_and_phone():
    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, linkedin_url=LINKEDIN, email=EMAIL)
        await record_phone_lookup(s, person.id, phone="+14155552671")
        await s.commit()
        view = await read_person(s, person.id)

    assert view.email == EMAIL
    assert view.phone == "+14155552671"
    assert view.phone_status == "found"


# ---- shared enrichment ----------------------------------------------------------------------------

async def test_a_miss_is_recorded_so_it_is_not_re_purchased():
    """A `not_found` is an expensive answer. Without persisting it, the same empty paid lookup is
    bought again on every crawl — the difference between a bounded bill and an unbounded one."""
    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, linkedin_url=LINKEDIN)
        stored = await record_phone_lookup(s, person.id)
        await s.commit()
        view = await read_person(s, person.id)

    assert stored is False
    assert view.phone_status == "not_found"
    assert view.last_enriched_at is not None, "a miss still counts as having looked"


async def test_an_unparseable_number_is_kept_sealed_rather_than_dropped():
    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, linkedin_url=LINKEDIN)
        await record_phone_lookup(s, person.id, raw="ext 4 via reception")
        await s.commit()
        view = await read_person(s, person.id)

    assert view.phone == ""
    assert view.phone_raw == "ext 4 via reception"
    assert view.phone_status == "found"


# ---- erasure ---------------------------------------------------------------------------------------

async def test_forgetting_a_person_removes_the_row_and_its_identities():
    """A shared store makes erasure easier: one row, not N deletes across N workspaces with no
    list of which ones hold the person."""
    from sqlalchemy import func, select

    from nexus.models.person import PersonIdentity

    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, linkedin_url=LINKEDIN, email=EMAIL)
        await s.commit()
        pid = person.id
    async with get_platform_sessionmaker()() as s:
        assert await forget_person(s, pid) is True
        await s.commit()
    async with get_platform_sessionmaker()() as s:
        assert await read_person(s, pid) is None
        left = await s.scalar(
            select(func.count()).select_from(PersonIdentity).where(
                PersonIdentity.person_id == pid
            )
        )
    assert left == 0, "identities must go with the person, not linger as orphans"


async def test_forgetting_an_unknown_person_is_not_an_error():
    async with get_platform_sessionmaker()() as s:
        assert await forget_person(s, "does-not-exist") is False


# ---- tenancy ----------------------------------------------------------------------------------------

async def test_the_people_tables_carry_no_tenant_id():
    """apply_rls.py enrols any table with tenant_id. Enrolling these would return zero rows to the
    shared resolver — silent under RLS, not an error."""
    from nexus.models.person import Person, PersonIdentity

    for model in (Person, PersonIdentity):
        assert "tenant_id" not in model.__table__.columns, model.__tablename__
