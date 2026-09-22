"""Sortable unique ids (ULID) for ledger events and outbound message references.

A ULID is 48 bits of milliseconds followed by 80 random bits, written as 26 Crockford base32
characters. Sorting the strings sorts by creation time, which is what the ledger outbox and the
archive store rely on when they batch "everything after the last shipped event".
"""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_ALPHABET)}
_MAX_MS = (1 << 48) - 1


def new_ulid(now_ms: int | None = None) -> str:
    """A new ULID. ``now_ms`` pins the timestamp part (used by tests and backfills)."""
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if ms < 0 or ms > _MAX_MS:
        raise ValueError(f"timestamp {ms} is outside the ULID range")
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def ulid_timestamp_ms(ulid: str) -> int:
    """The millisecond timestamp a ULID carries. Raises ``ValueError`` on anything malformed."""
    text = (ulid or "").strip().upper()
    if len(text) != 26 or any(c not in _DECODE for c in text):
        raise ValueError(f"not a ULID: {ulid!r}")
    value = 0
    for c in text[:10]:
        value = (value << 5) | _DECODE[c]
    return value
