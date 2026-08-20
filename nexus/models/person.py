# nexus/models/person.py
"""Shared person records — one row per real human, across every tenant.

The companion to ``nexus/models/company.py``. A ``Contact`` is per-tenant, so forty workspaces
tracking the same VP Engineering means forty rows and forty paid phone lookups for one phone number.

**Platform-global on purpose: no ``tenant_id``.** ``scripts/apply_rls.py`` enrols any table that has
one, and enrolling these would return zero rows to the shared resolver — the failure mode CLAUDE.md
warns about, where an RLS miss looks like "no data" rather than an error. Everything here runs
through ``get_platform_sessionmaker()``.

**Email and phone are encrypted at rest; their hashes are not.** That split is the whole design:

* A **hash** column is what lookups use. "Do we already know this email?" is an indexed equality
  check on ``sha256(normalised)`` and never decrypts anything. Sealed ciphertext cannot be indexed —
  Fernet is randomised, so the same email seals to a different blob every time and an index over it
  would match nothing.
* The **sealed** column is what a rep actually reads, decrypted at the point of use.

A hash is not anonymisation: the space of email addresses is small enough to brute-force, so the
hash is an index, not a privacy control. The encryption is what protects the value if the database
is copied.

Name, title and company domain are deliberately **not** encrypted. They are business-card facts, the
product has to search and match on them, and encrypting them would break resolution while protecting
nothing that a LinkedIn search does not already give away.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime

# How a person record came to exist, for provenance when two sources disagree. `source_db` is a
# registered external source database (`nexus/sources/`), read ahead of the paid providers.
PERSON_SOURCES = ("contact_backfill", "enrichment", "network", "import", "source_db")

# Whether a paid lookup has been attempted for this person's phone. `not_found` is a real,
# expensive answer and is recorded so the same miss is not re-purchased every crawl — the
# distinction between "we have not looked" and "we looked and there is nothing" is the difference
# between a bounded bill and an unbounded one.
PHONE_STATUSES = ("unattempted", "found", "not_found", "failed")

# What kind of key an identity row carries.
IDENTITY_KINDS = ("email", "linkedin", "phone")


class Person(TimestampMixin, Base):
    __tablename__ = "people"
    __table_args__ = (
        # "Who is this?" — the two lookups the resolver actually makes.
        Index("ix_person_email_hash", "email_hash"),
        Index("ix_person_linkedin", "linkedin_url"),
        # "What is due for a shared enrichment refresh?" — the enricher's only scan.
        Index("ix_person_enrich_due", "last_enriched_at"),
    )

    # sha1 of the resolution key. Deterministic, so two workers racing on the same person produce
    # the same primary key: one insert wins, the other re-reads, instead of both succeeding and
    # splitting one human into two records.
    id: Mapped[str] = mapped_column(String(40), primary_key=True)

    # --- searchable, unencrypted: business-card facts -------------------------------------------
    full_name: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Not a FK to companies: a person can be resolved before their employer is, and a hard link
    # would make person creation fail on an unknown company.
    company_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Identity, and the input the phone-finder actor takes. Not secret — it is a public profile URL.
    linkedin_url: Mapped[str | None] = mapped_column(String(400), nullable=True)

    # --- encrypted values, with hashes for lookup ------------------------------------------------
    # sha256 of the normalised email. Indexed; never reversed by us.
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The original string when it could not be parsed to E.164. Sealed for the same reason the
    # canonical form is: it is still a phone number. Kept rather than discarded so a normalisation
    # gap never costs the only contact detail we have for someone.
    phone_raw_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- shared enrichment state -----------------------------------------------------------------
    # The point of sharing: one paid lookup serves every tenant tracking this person.
    phone_status: Mapped[str] = mapped_column(String(20), default="unattempted", index=True)
    email_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_enriched_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    enrichment_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="contact_backfill")


class PersonIdentity(IdMixin, TimestampMixin, Base):
    """An alternate key that resolves to a person.

    People have several emails (work, personal, a previous employer's) and one LinkedIn URL. Putting
    those on ``people`` as columns would cap the number we can hold and make "have we seen this
    address anywhere?" a scan across several columns instead of one indexed lookup.

    Stores only the **hash**. The value itself lives sealed on ``people``; an identity row exists to
    answer "which person is this?", which never requires reading the value back.
    """

    __tablename__ = "person_identities"
    __table_args__ = (
        # One hash resolves to exactly one person. The database enforces it, so a race that tries
        # to attach the same email to two people fails loudly instead of silently forking a human.
        UniqueConstraint("kind", "value_hash", name="uq_person_identity_value"),
        Index("ix_person_identity_person", "person_id"),
    )

    person_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))       # email | linkedin | phone
    value_hash: Mapped[str] = mapped_column(String(64))
