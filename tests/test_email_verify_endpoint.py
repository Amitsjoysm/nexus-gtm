# tests/test_email_verify_endpoint.py
"""The verifier endpoint shipped as a default has to be the one that answers.

`email_verify_url` defaulted to `158.69.113.127`, a host that does not respond. Measured
2026-08-27: `.127` times out after 21s and returns nothing; `.104` answers in ~1s with a full
verdict. Any deployment that did not override the setting therefore ran a verifier that could
only ever produce `unknown`, and because the Reacher client is (correctly) fail-safe, the outage
was indistinguishable from "we looked and could not tell".

A dead default is worse than no default: no default fails loudly at startup, a dead one fails
silently on every address forever.
"""
from __future__ import annotations

import re

from nexus.core.config import Settings


VERIFIER_HOST = "158.69.113.104"


def test_the_default_verifier_endpoint_is_the_live_host():
    url = Settings().email_verify_url
    assert VERIFIER_HOST in url, (
        f"default email_verify_url is {url!r}; the answering Reacher host is {VERIFIER_HOST}"
    )
    assert url.endswith("/v0/check_email"), f"not a Reacher check endpoint: {url!r}"


def test_no_stale_verifier_host_survives_anywhere_in_the_tree():
    """The dead host was in the code default, the deploy env and the examples independently."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    stale = re.compile(r"158\.69\.113\.(?!104\b)\d+")
    offenders: list[str] = []
    for path in list(root.glob("nexus/**/*.py")) + list(root.glob("deploy/**/*.example")):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if stale.search(text):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"a superseded verifier host is still referenced in: {offenders}"
