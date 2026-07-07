# nexus/network/linkedin_csv.py
"""Parse a member's official LinkedIn data export (``Connections.csv``) into RawIdentity rows.

LinkedIn has no API to read a member's connections within ToS; the compliant path is the member's
own export ("Settings → Get a copy of your data → Connections"). The file starts with a short
"Notes:" preamble before the real header row, which we skip.
"""
from __future__ import annotations

import csv
import io

from nexus.network.connectors.base import RawIdentity

_REQUIRED = {"First Name", "Last Name", "Company", "Position"}
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — a real Connections export is far smaller; cap misuse/DoS.


class LinkedInCsvError(ValueError):
    """Raised when the upload is not a recognizable LinkedIn Connections export."""


def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_linkedin_csv(content: bytes) -> list[RawIdentity]:
    if len(content) > _MAX_BYTES:
        raise LinkedInCsvError("file too large (max 10 MB)")
    text = _decode(content)
    lines = text.splitlines()
    # Find the header row (LinkedIn prepends a Notes preamble + a blank line).
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("First Name,Last Name,")), None
    )
    if header_idx is None:
        raise LinkedInCsvError("not a LinkedIn Connections export (missing header row)")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    if not _REQUIRED.issubset(set(reader.fieldnames or [])):
        raise LinkedInCsvError("LinkedIn export is missing expected columns")

    out: list[RawIdentity] = []
    for i, row in enumerate(reader):
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        name = " ".join(p for p in (first, last) if p)
        if not name:
            continue
        email = (row.get("Email Address") or "").strip() or None
        url = (row.get("URL") or "").strip()
        out.append(
            RawIdentity(
                external_id=url or f"linkedin:{name.lower()}:{i}",
                email=email,
                name=name,
                title=(row.get("Position") or "").strip() or None,
                company=(row.get("Company") or "").strip() or None,
                handle=url or None,
                relation="linkedin_1st",
            )
        )
    return out
