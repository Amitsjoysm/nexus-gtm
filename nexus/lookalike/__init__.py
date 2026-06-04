"""Find-lookalike-companies: seed a won account's URL, get similar companies, score them."""
from nexus.lookalike.service import (
    Lookalike,
    LookalikeService,
    get_lookalike_service,
    set_lookalike_service,
)

__all__ = [
    "Lookalike",
    "LookalikeService",
    "get_lookalike_service",
    "set_lookalike_service",
]
