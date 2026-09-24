"""LinkedIn industry codes (v2), and turning an ICP's industries into them.

The company-search actor filters on LinkedIn's numeric industry codes. The 434 of them are vendored
in ``nexus/data/linkedin_industries_v2.csv`` (from HarvestAPI/linkedin-industry-codes-v2) and loaded
once into memory: 434 fixed rows are faster from a cache than from a table, and they never change
per tenant.

``map_industries`` runs when an ICP is saved and its result is stored on the ICP, so a search never
re-maps. Three rules, each from measuring the real list:

* **Whole words, never substrings.** "artificial" is a substring of *Artificial Rubber and Synthetic
  Fiber Manufacturing*, so a substring match filed AI companies under rubber.
* **Synonyms first.** "SaaS", "fintech", "cyber", "e-commerce" and "AI" match no label at all.
* **The LLM only for misses, from a shortlist.** It sees ~20 labels ranked for the term, never all 434
  with descriptions, and an id it returns that is not on the shortlist is discarded.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("nexus.prospecting.industries")

_CSV = Path(__file__).resolve().parent.parent / "data" / "linkedin_industries_v2.csv"
SHORTLIST_SIZE = 20
MAX_PER_TERM = 3

#: Terms no label says, mapped by hand. High precision only: each entry is a promise that a
#: company described this way is filed under these codes on LinkedIn.
SYNONYMS: dict[str, tuple[int, ...]] = {
    "saas": (4,),
    "software": (4,),
    "b2b software": (4,),
    "software and saas": (4,),
    "cybersecurity": (118,),
    "cyber security": (118,),
    "information security": (118,),
    "infosec": (118,),
    "ecommerce": (1445,),
    "e commerce": (1445,),
    "online retail": (1445,),
    "fintech": (43,),
    "ai": (4, 6),
    "artificial intelligence": (4, 6),
    "machine learning": (4, 6),
    "it services": (96,),
    "managed services": (96,),
    "msp": (96,),
    "healthcare": (14,),
    "health care": (14,),
    "staffing": (104,),
    "recruiting": (104,),
    "recruitment": (104,),
    "martech": (4,),
    "edtech": (1999,),
}

_WORD = re.compile(r"[a-z0-9]+")
# Connectives that carry no meaning in a label ("Hospitals and Health Care").
_STOP = frozenset({"and", "of", "the", "for", "in", "or", "a", "an", "other"})


@dataclass(frozen=True)
class Industry:
    id: int
    label: str
    hierarchy: str
    description: str

    @property
    def depth(self) -> int:
        return self.hierarchy.count(">")


def _norm(text: str) -> str:
    text = (text or "").lower().replace("&", " and ").replace("-", " ")
    return " ".join(_WORD.findall(text))


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(_norm(text)) if w not in _STOP)


@lru_cache(maxsize=1)
def all_industries() -> dict[int, Industry]:
    """Every LinkedIn v2 industry, by id. Loaded once per process."""
    with _CSV.open(encoding="utf-8", newline="") as fh:
        return {
            int(row["id"]): Industry(
                id=int(row["id"]), label=row["label"].strip(),
                hierarchy=row["hierarchy"].strip(), description=(row.get("description") or "").strip(),
            )
            for row in csv.DictReader(fh)
            if (row.get("id") or "").strip().isdigit()
        }


@lru_cache(maxsize=1)
def _by_norm_label() -> dict[str, int]:
    return {_norm(i.label): i.id for i in all_industries().values()}


def descendants(ids) -> set[int]:
    """These codes and every code filed beneath them — for matching stored companies, whose code is
    often a child ("Capital Markets") of the ICP's ("Financial Services")."""
    wanted = {all_industries()[i].label for i in ids if i in all_industries()}
    out = set(ids)
    for industry in all_industries().values():
        path = [p.strip() for p in industry.hierarchy.split(">")]
        if wanted.intersection(path):
            out.add(industry.id)
    return out


def match_term(term: str) -> list[int]:
    """Deterministic codes for ONE term, or [] when nothing matches cleanly (then the LLM is asked)."""
    key = _norm(term)
    if not key:
        return []
    if key in _by_norm_label():
        return [_by_norm_label()[key]]
    if key in SYNONYMS:
        return list(SYNONYMS[key])
    wanted = _tokens(term)
    if not wanted:
        return []
    # Whole-word containment: every word of the term appears in the label. The label with the fewest
    # extra words wins, then the shallowest (the parent over a narrow child).
    candidates = [
        (len(_tokens(i.label) - wanted), i.depth, i.id)
        for i in all_industries().values()
        if wanted <= _tokens(i.label)
    ]
    if not candidates:
        return []
    candidates.sort()
    return [candidates[0][2]]


def split_terms(values) -> list[str]:
    """ICP values may be compound ("Software & SaaS"). Split on & , / ; — never on "and", which
    labels themselves contain ("Food and Beverage Services")."""
    out: list[str] = []
    for value in values or []:
        text = str(value or "")
        if _norm(text) in _by_norm_label():
            out.append(text)
            continue
        out.extend(p.strip() for p in re.split(r"\s*(?:&|,|/|;|\+)\s*", text) if p.strip())
    return out


def shortlist_for(term: str) -> list[Industry]:
    """The labels worth showing an LLM for this term: ranked by words shared with the label and
    hierarchy (and, weakly, the description). ~20, not 434."""
    wanted = _tokens(term)

    def score(i: Industry) -> tuple:
        label_hits = len(wanted & _tokens(i.label))
        path_hits = len(wanted & _tokens(i.hierarchy))
        desc_hits = len(wanted & _tokens(i.description))
        return (-(label_hits * 3 + path_hits * 2 + desc_hits), i.depth, i.id)

    ranked = sorted(all_industries().values(), key=score)
    return ranked[:SHORTLIST_SIZE]


LLMPicker = Callable[[str, list[Industry]], Awaitable[str]]


async def _default_llm(term: str, shortlist: list[Industry]) -> str:
    from nexus.agents.llm import LLMMessage, get_llm_provider

    options = "\n".join(f"{i.id}: {i.label}" for i in shortlist)
    prompt = (
        f"Which LinkedIn industries best describe companies in the category '{term}'? "
        f"Answer ONLY with a JSON array of at most {MAX_PER_TERM} ids from this list:\n{options}"
    )
    resp = await get_llm_provider().complete(
        [LLMMessage(role="user", content=prompt)], purpose="industry_mapping", max_tokens=40,
    )
    return resp.text


def _parse_ids(text: str) -> list[int]:
    match = re.search(r"\[[^\]]*\]", text or "")
    if not match:
        return []
    try:
        return [int(x) for x in json.loads(match.group(0)) if str(x).strip().isdigit()]
    except (ValueError, TypeError):
        return []


async def map_industries(values, *, llm: LLMPicker | None = None) -> list[int]:
    """LinkedIn codes for an ICP's industry list: deterministic first, the LLM only for misses.

    Never raises: a term the LLM cannot place costs that term, not the whole mapping.
    """
    picker = llm or _default_llm
    ids: list[int] = []
    for term in split_terms(values):
        found = match_term(term)
        if not found:
            shortlist = shortlist_for(term)
            allowed = {i.id for i in shortlist}
            try:
                found = [i for i in _parse_ids(await picker(term, shortlist)) if i in allowed]
            except Exception as exc:
                logger.info("industry mapping for %r fell back to nothing: %r", term, exc)
                found = []
            found = found[:MAX_PER_TERM]
        ids.extend(i for i in found if i not in ids)
    return ids
