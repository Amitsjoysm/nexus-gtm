"""Email subjects: one base subject per conversation, and exactly one "Re:" on replies (D16).

Mail clients prepend a reply or forward marker in the reader's language, and many prepend again on
every round trip: "RE: Re: AW: Quick question". A follow-up that simply adds "Re: " to whatever
arrived produces the stack the product owner ruled out. So every outbound follow-up and response is
built from the BASE subject — the text with every leading marker and mail-system tag removed — with
one "Re: " in front.

The marker list covers the clients SDRs actually receive from: English (Re, Fw, Fwd), German (AW,
WG), Nordic (SV, VS), Dutch (Antw), French (TR), Spanish (RV), Italian (R), Polish (Odp), Turkish
(Ynt), numbered forms (Re[2], Re(3)) and a space before the colon ("RE :"). Corporate gateways add
an "[EXTERNAL]" tag to inbound mail, and the buyer's reply carries it back, so those tags are
stripped too.
"""
from __future__ import annotations

import re

_MARKER = re.compile(
    r"""^\s*(?:
        (?:re|fw|fwd|aw|wg|sv|vs|antw|tr|rv|r|odp|ynt)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]
      | \[\s*(?:external|ext|external\s+email|external\s+sender)\s*\]
      | \*\s*external\s*\*
      | external\s*:
    )\s*""",
    re.IGNORECASE | re.VERBOSE,
)
_SPACES = re.compile(r"\s+")


def normalize_subject(subject: str | None) -> str:
    """The subject with every leading reply/forward marker and gateway tag removed."""
    text = _SPACES.sub(" ", str(subject or "")).strip()
    while True:
        stripped = _MARKER.sub("", text, count=1)
        if stripped == text:
            return text
        text = stripped.strip()


def reply_subject(subject: str | None) -> str:
    """The subject for a follow-up or response: exactly one "Re: " before the base subject."""
    base = normalize_subject(subject)
    return f"Re: {base}" if base else "Re:"
