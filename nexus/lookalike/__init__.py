"""Find-lookalike-companies and -people: seed a won account/contact, get similar ones, score them."""
from nexus.lookalike.contacts import (
    ContactLookalike,
    ContactLookalikeService,
    get_contact_lookalike_service,
    set_contact_lookalike_service,
)
from nexus.lookalike.service import (
    Lookalike,
    LookalikeService,
    get_lookalike_service,
    set_lookalike_service,
)
from nexus.lookalike.similarity import (
    company_similarity,
    contact_similarity,
    prepare_company,
    prepare_contact,
)

__all__ = [
    "Lookalike",
    "LookalikeService",
    "get_lookalike_service",
    "set_lookalike_service",
    "ContactLookalike",
    "ContactLookalikeService",
    "get_contact_lookalike_service",
    "set_contact_lookalike_service",
    "company_similarity",
    "contact_similarity",
    "prepare_company",
    "prepare_contact",
]
