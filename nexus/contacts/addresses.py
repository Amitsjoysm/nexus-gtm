"""A contact's email address is ONE recipient (security fix, 2026-11-20).

Proved on master 6dae3145: ``blocked@target.com, a@other.org, b@other.org`` was accepted as one
contact's address, the sender put the whole string in ``To:`` (four recipients, one charge), and
the do-not-contact check looked the whole string up, matched nothing, and let it through. A comma, a
semicolon, a space or a line break in the field is how one "contact" becomes a mailing list.

``single_address`` is the one rule. It is applied where a person types or uploads an address (the
contact endpoints, the CSV import) and again at every send seam (the engagement sender, the SMTP
sender), because rows written before the check, or by a path that never validated, must still stop
there. It accepts ``Jane Buyer <jane@acme.io>``, which some exports write, and gives back the bare
address; anything holding more than one address, or none, is ``None``.

Deliberately strict rather than RFC 5322 complete: quoted local parts, comments and IP-literal
domains are refused. None is a real prospect's work address, and each is a way to smuggle a second
recipient past a looser parser.
"""
from __future__ import annotations

import re

#: ``Name <address>`` with nothing after the closing bracket. The name may not hold another
#: address or a separator, or ``a@x.com <b@y.com>`` would pass as one.
_NAMED = re.compile(r'^\s*"?([^"<>,;@\r\n]*)"?\s*<([^<>\s]+)>\s*$')
_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}$")
_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def single_address(raw: str | None) -> str | None:
    """The one address ``raw`` holds, stripped, case kept; ``None`` when it holds more or none."""
    text = (raw or "").strip()
    if not text:
        return None
    named = _NAMED.match(text)
    if named:
        text = named.group(2).strip()
    if any(ch in text for ch in ",; \t\r\n<>\"()[]\\") or text.count("@") != 1:
        return None
    local, _, domain = text.partition("@")
    if not _LOCAL.match(local) or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(_LABEL.match(part) for part in labels):
        return None
    if labels[-1].isdigit():  # 10.0.0.5 is not a mail domain
        return None
    return text


def is_single_address(raw: str | None) -> bool:
    return single_address(raw) is not None
