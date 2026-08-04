# nexus/people/__init__.py
"""Shared person records: resolve once, enrich once, serve every tenant.

The companion to ``nexus/companies/``. The measured motivation is the same shape: a phone lookup is
a paid actor run, and forty workspaces tracking one VP Engineering pay for it forty times.

**Identity is an explicit key, never a fuzzy match.** In priority order: LinkedIn URL, then
normalised email. A name plus a company is *not* an identity here — this codebase has already
shipped six wrong-attribution bugs in the signal subsystem by trusting a name match, and the blast
radius of getting a *person* wrong is a rep phoning a stranger with someone else's context.

A contact with neither a LinkedIn URL nor an email therefore gets **no** shared person and stays
entirely per-tenant. That is the safety property, not a gap to close later.

Read ``nexus/people/store.py`` for the encryption split (hash to find, sealed to read) and
``erasure.py`` for why a shared store makes deletion easier rather than harder.
"""
from nexus.people.store import (
    PersonView,
    forget_person,
    hash_value,
    person_id_for,
    read_person,
    record_phone_lookup,
    resolve_person_record,
)

__all__ = [
    "PersonView",
    "forget_person",
    "hash_value",
    "person_id_for",
    "read_person",
    "record_phone_lookup",
    "resolve_person_record",
]
