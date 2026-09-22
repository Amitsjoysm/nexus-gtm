"""Signed unsubscribe links (D11, RFC 8058).

A link must keep working for as long as someone might click it in an old email, so it carries no
expiry. It carries the workspace and the contact, and an HMAC over both keyed from ``secret_key``;
nothing in it is guessable, and changing either id breaks the signature. It does NOT carry the email
address: links are logged by proxies and mail scanners, and the address is found from the contact.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from nexus.core.config import get_settings

_VERSION = "v1"
_SHAPE = re.compile(r"v1\.([0-9a-f]{32})\.([0-9a-f]{32})\.([0-9a-f]{32})")


def _signature(tenant_id: str, contact_id: str) -> str:
    key = hashlib.sha256(f"engagement:unsubscribe:{get_settings().secret_key}".encode()).digest()
    message = f"{_VERSION}|{tenant_id}|{contact_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def make_token(tenant_id: str, contact_id: str) -> str:
    return f"{_VERSION}.{tenant_id}.{contact_id}.{_signature(tenant_id, contact_id)}"


def read_token(token: str) -> tuple[str, str] | None:
    """``(tenant_id, contact_id)`` for a genuine token, else ``None``."""
    match = _SHAPE.fullmatch((token or "").strip())
    if match is None:
        return None
    tenant_id, contact_id, signature = match.groups()
    if not hmac.compare_digest(signature, _signature(tenant_id, contact_id)):
        return None
    return tenant_id, contact_id


def unsubscribe_url(tenant_id: str, contact_id: str) -> str:
    from nexus.engagement.config import public_base_url

    return f"{public_base_url()}/api/u/{make_token(tenant_id, contact_id)}"
