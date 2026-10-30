"""Which of our value props meet this person's likely pains, for the call console (6.3).

Deterministic, no LLM: the console is read out loud, and a mapping the rep cannot trace back to the
workspace's own value props is a sentence they cannot defend on the call. What it does is narrow:

- The pains are the WORKSPACE's (`value_props[].pains_solved`), never invented for the person.
- A pain is "theirs" only when it shares a content word with what we know about them: their title,
  the role angle `personalization.brief` derives from it, the account's industry and the signals the
  brief cites. That is overlap, not proof, and the console labels it as a likely pain.
- Value props are ranked by how many of their pains matched, then kept in the workspace's own
  order, so with nothing known about the person the list is simply the workspace's first three.
"""
from __future__ import annotations

import re

#: How many value props the console shows. The panel shares a column at 1366x768.
MAX_PROPS = 3

_WORD = re.compile(r"[a-z][a-z0-9]+")
# Words long enough to pass the length floor that still say nothing about a pain.
_STOP = frozenset({
    "about", "across", "after", "also", "and", "are", "based", "being", "brief", "busy", "does", "doing", "each", "every", "first", "from", "have", "head", "into", "keep", "lead",
    "level", "like", "make", "making", "more", "most", "much", "need", "only", "over", "own",
    "role", "same", "senior", "some", "such", "team", "teams", "than", "that", "their", "them",
    "then", "they", "this", "time", "very", "what", "when", "where", "which", "while", "with",
    "without", "work", "your",
})


def _stem(word: str) -> str:
    """Enough folding that "forecasts" meets "forecast" and "hiring" meets "hire"; no more."""
    for suffix in ("ing", "ies", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def content_words(text: str | None) -> set[str]:
    return {_stem(w) for w in _WORD.findall((text or "").lower())
            if len(w) >= 4 and w not in _STOP}


def value_fit(value_props: list, *, person_context: list[str | None]) -> list[dict]:
    """Up to `MAX_PROPS` value props, each with its pains and which of them look like this
    person's, best first. Value props without a name are skipped; nothing raises."""
    known: set[str] = set()
    for text in person_context:
        known |= content_words(text)
    ranked: list[tuple[int, int, dict]] = []
    for position, raw in enumerate(value_props or []):
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            continue
        pains = [str(p).strip() for p in (raw.get("pains_solved") or []) if str(p).strip()]
        matched = [p for p in pains if content_words(p) & known]
        ranked.append((len(matched), position, {
            "name": str(raw["name"]).strip(),
            "description": str(raw.get("description") or "").strip(),
            "pains": pains,
            "matched": matched,
        }))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked[:MAX_PROPS]]
