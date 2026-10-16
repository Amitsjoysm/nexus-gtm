"""The Net new cursor: where a scan stopped, signed so it cannot be edited or replayed elsewhere.

A Net new page is filled by scanning the provider's pages and skipping rows the workspace holds, so
"page 3" has no meaning; the next click resumes from (API page, offset). The cursor carries that plus
a hash of the filters it was made for, signed with an HMAC over a key derived from ``secret_key``
and bound to the tenant. A cursor from another workspace, an edited one, or one replayed with
different filters is refused rather than trusted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from nexus.prospector.errors import ProspectorError

_EXPIRED = "This scan position is no longer valid. Run the search again."


def _key() -> bytes:
    from nexus.core.config import get_settings

    return hashlib.sha256(f"prospector-cursor|{get_settings().secret_key}".encode()).digest()


def _sign(tenant_id: str, body: str) -> str:
    return hmac.new(_key(), f"{tenant_id}|{body}".encode(), hashlib.sha256).hexdigest()[:24]


def query_hash(view: str, canonical: dict) -> str:
    raw = json.dumps({"v": view, "f": canonical}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def encode(tenant_id: str, data: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(data, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")
    return f"{body}.{_sign(tenant_id, body)}"


def decode(tenant_id: str, token: str, *, expect_query: str) -> dict:
    try:
        body, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(tenant_id, body)):
            raise ValueError("signature")
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data.get("q") != expect_query:
            raise ValueError("filters changed")
        page, offset, size = int(data["p"]), int(data["o"]), int(data["s"])
        if not (1 <= page <= 10_000 and 0 <= offset < size <= 500):
            raise ValueError("range")
        return {"p": page, "o": offset, "s": size, "q": data["q"]}
    except Exception:
        raise ProspectorError(400, _EXPIRED) from None
