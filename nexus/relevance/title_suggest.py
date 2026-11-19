"""Buyer titles that fit the offer (W4, SDR feedback 2026-10-29).

Reported: "Suggest titles" returned generic CXO and CEO titles and ignored the Title keywords and
Exclude keywords the SDR had typed. It ranked a fixed catalogue of ~25 roles and read neither field.

Now the LLM is asked with EVERYTHING the ICP (or the service's ICP) says: what is sold and the pains
it removes, industries, size, countries, required tech, the title keywords, the exclude keywords,
the job levels, and the titles already listed. Each title comes back with a one-line reason.

**A rule in a prompt is a request; the filter below is the guarantee**, deterministic and the same
for the model's titles and the catalogue fallback:

* a title containing an exclude keyword is dropped, by the SAME substring rule contact search applies
  (`job_levels.matches_title`), so a suggested title is never one whose people the search refuses;
* a C-level title (CEO, Chief ..., Founder, Owner, President, Managing Director: `job_levels.level_of`)
  is dropped unless the job levels include C-level or the ICP's typical company has under 50 people,
  where the founder genuinely is the buyer;
* a title already listed is not suggested again.

With no model, a model that fails, or one whose every title is filtered out, the catalogue ranking
(`titles.recommend_titles_for_icp`) is used, through the same filter.

**Titles are for searching, so they must be spelled as people hold them** (2026-11-19, measured on
four workspaces' saved ICPs): the model padded every list to ten with roles that do not buy
(Procurement Specialist, Compliance Officer, CRM Integration Engineer), wrote "Account‑Based" with
a non-breaking hyphen (U+2011) that no database title matches, added bracketed abbreviations
("Chief Marketing Officer (CMO)") and suggested again what the list already held in other words.
So every title is cleaned (`clean_title`), a repeat is judged by meaning (`_meaning`, the token rule
`gate.same_meaning_title` uses), a support role is dropped unless the offer is about its function
(`_peripheral`), budget owners and champions rank above end users, and the prompt asks for fewer,
real titles rather than a full list.
"""
from __future__ import annotations

import json
import logging
import re

from nexus.relevance.job_levels import _normalise, level_of, matches_title
from nexus.relevance.titles import TitleRecommendation, ranked_titles_for_icp

logger = logging.getLogger("nexus.relevance.title_suggest")

#: Below this many employees the founder or CEO is usually who buys, so C-level is allowed.
SMALL_COMPANY = 50
MAX_SUGGESTIONS = 10
_INFLUENCES = ("economic_buyer", "champion", "technical_evaluator", "end_user")
#: Rank within a list: who owns the budget, who pushes for it, who checks it, who uses it.
_INFLUENCE_RANK = {name: i for i, name in enumerate(_INFLUENCES)}
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_BRACKETED = re.compile(r"\s*[(\[][^)\]]*[)\]]")
#: Functions that process or police a purchase rather than make it. Dropped unless the offer, the
#: industries or the title keywords name the function (compliance software IS bought by compliance).
_PERIPHERAL = {
    # Stems that mean the offer is FOR this function. Not "buyer" or "risk": a sales offer says
    # "buyer intent" and most offers "reduce risk", which would keep every procurement and
    # compliance title they are meant to drop.
    "procurement": ("procurement", "purchasing", "sourcing", "supplier"),
    "purchasing": ("procurement", "purchasing", "sourcing", "supplier"),
    "legal": ("legal", "lawyer", "law firm", "counsel"),
    "counsel": ("legal", "lawyer", "law firm", "counsel"),
    "paralegal": ("legal", "lawyer", "law firm", "paralegal"),
    "compliance": ("compliance", "regulat", "audit", "governance"),
    "intern": ("intern",),
    "trainee": ("trainee", "training"),
    "student": ("student", "education", "universit", "school"),
    "receptionist": ("reception", "front desk"),
}


#: Kept in capitals when a model writes a title all in lower case.
_ACRONYMS = frozenset({
    "vp", "svp", "evp", "avp", "ceo", "cfo", "cto", "cio", "cmo", "coo", "cro", "cdo", "cpo",
    "ciso", "cso", "it", "hr", "qa", "ux", "ui", "seo", "abm", "crm", "erp", "saas", "b2b", "gtm",
    "emea", "apac", "uk", "us", "eu", "ai", "ml", "r&d", "hse", "ehs", "pr", "bi",
})
_SMALL_WORDS = frozenset({"of", "and", "for", "the", "in", "to", "at", "or", "a", "an"})


def _smart_case(title: str) -> str:
    """A title the model wrote all in lower case, capitalised as people write it. Any other casing
    is the model's own and is kept."""
    if not title.islower():
        return title
    out = []
    for i, word in enumerate(title.split(" ")):
        if word in _ACRONYMS:
            out.append(word.upper())
        elif i and word in _SMALL_WORDS:
            out.append(word)
        else:
            out.append("-".join(part[:1].upper() + part[1:] for part in word.split("-")))
    return " ".join(out)


def clean_title(raw) -> str:
    """A title as a database holds it: look-alike characters made plain ("Account‑Based" with a
    non-breaking hyphen matches no stored title), no bracketed abbreviation ("(CMO)"), one space,
    capitalised when the model wrote it all in lower case."""
    from nexus.agents.copy import tidy_text

    text = _BRACKETED.sub("", tidy_text(str(raw or "")))
    return _smart_case(re.sub(r"\s+", " ", text).strip(" -–—,;:/|")[:120])


def _meaning(title: str) -> frozenset[str]:
    """What a title says, in any word order or abbreviation: "VP Sales" is "Vice President of
    Sales". The token rule `gate.same_meaning_title` uses."""
    from nexus.relevance.gate import _title_tokens

    return _title_tokens(clean_title(title))


def _peripheral(title: str, icp: dict) -> bool:
    """Is this a support role (procurement, legal, compliance, an intern) for an offer that is not
    about its function?"""
    words = set(_normalise(title).split())
    hits = [about for word, about in _PERIPHERAL.items() if word in words]
    if not hits:
        return False
    offer = _normalise(" ".join([
        str(icp.get("product_context") or ""),
        *[f"{vp.get('name', '')} {vp.get('description', '')} {' '.join(vp.get('pains_solved') or [])}"
          for vp in (icp.get("value_props") or []) if isinstance(vp, dict)],
        *[str(x) for x in (icp.get("industries") or [])],
        *[str(x) for x in (icp.get("title_keywords") or [])],
    ]))
    return not any(stem in offer for about in hits for stem in about)


def typical_size(icp: dict) -> int | None:
    """The ICP's typical company size: the middle of the band, or whichever end is given."""
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    try:
        lo = int(lo) if lo not in (None, "") else None
        hi = int(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if lo is not None and hi is not None:
        return (lo + hi) // 2
    return hi if hi is not None else lo


def c_level_allowed(icp: dict) -> bool:
    levels = {str(x).strip().lower() for x in (icp.get("job_levels") or [])}
    if "c_level" in levels:
        return True
    size = typical_size(icp)
    return size is not None and size < SMALL_COMPANY


def keep(title: str, icp: dict, seen: set[frozenset]) -> bool:
    """Does ``title`` survive the filter? ``seen`` holds the meanings (`_meaning`) of the titles
    already listed or already kept, and is added to."""
    meaning = _meaning(title)
    if not meaning or meaning in seen:
        return False
    excluded = [x for x in (icp.get("exclude_title_keywords") or []) if str(x).strip()]
    if excluded and not matches_title(title, {"exclude_title_keywords": excluded}):
        return False
    if level_of(title) == "c_level" and not c_level_allowed(icp):
        return False
    if _peripheral(title, icp):
        return False
    seen.add(meaning)
    return True


def _listed(icp: dict) -> set[frozenset]:
    listed = icp.get("buyer_titles") or icp.get("titles") or []
    if isinstance(listed, str):
        listed = [listed]
    return {m for m in (_meaning(str(t)) for t in listed) if m}


def _bullets(values) -> str:
    return ", ".join(str(v).strip() for v in (values or []) if str(v).strip())


def build_prompt(icp: dict, *, limit: int) -> tuple[str, str]:
    """``(system, user)`` for the model. Every field the ICP holds is in it."""
    system = (
        "You are a B2B sales strategist who knows how buying committees work. Suggest the job "
        "titles of the people who would BUY, CHAMPION or EVALUATE this specific offer at the "
        "companies described: the people who feel the pains it solves. Be specific to the offer, "
        "never a generic list of executives. Fewer, better titles beat a full list: leave out "
        "anyone who only processes or polices a purchase (procurement, legal, compliance) or is "
        "only loosely touched by it, unless the offer is built for that function. These titles "
        "are used to SEARCH a contact database, so write each the way people hold it on their "
        "profile: plain words and plain hyphens, one role per title, no abbreviation in brackets, "
        "no slash. Output ONLY a JSON object, no prose, no code fences."
    )
    lines: list[str] = []
    context = str(icp.get("product_context") or "").strip()
    if context:
        lines.append(f"What is sold: {context[:1500]}")
    for vp in icp.get("value_props") or []:
        if isinstance(vp, dict) and str(vp.get("name") or "").strip():
            pains = _bullets(vp.get("pains_solved"))
            desc = str(vp.get("description") or "").strip()
            lines.append(f"Value proposition: {vp['name']}" + (f" ({desc})" if desc else "")
                         + (f"; pains it solves: {pains}" if pains else ""))
    for label, key in (("Target industries", "industries"), ("Countries", "countries"),
                       ("Required tech", "required_tech")):
        if _bullets(icp.get(key)):
            lines.append(f"{label}: {_bullets(icp.get(key))}")
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if lo or hi:
        lines.append(f"Company size: {lo or 1} to {hi or 'any number of'} employees")
    if _bullets(icp.get("title_keywords")):
        lines.append("Every title should be in one of these functions (title keywords): "
                     + _bullets(icp.get("title_keywords")))
    if _bullets(icp.get("exclude_title_keywords")):
        lines.append("Never suggest a title containing any of: "
                     + _bullets(icp.get("exclude_title_keywords")))
    levels = [str(x) for x in (icp.get("job_levels") or []) if str(x).strip()]
    if levels:
        lines.append("Seniority levels wanted: " + ", ".join(lv.replace("_", "-") for lv in levels))
    if not c_level_allowed(icp):
        lines.append("Do not suggest C-level titles (CEO, CFO, CTO, any Chief), Founder, Owner, "
                     "President or Managing Director: at companies this size they are not who "
                     "buys this.")
    if _bullets(icp.get("buyer_titles")):
        lines.append("Titles already on the list (do not repeat them): "
                     + _bullets(icp.get("buyer_titles")))
    user = (
        "\n".join(lines or ["(The ICP is empty: suggest the titles that typically buy B2B "
                            "software.)"])
        + f'\n\nReturn {{"titles": [{{"title": "...", "reason": "at most 15 words on why this '
        f'person buys or champions THIS offer", "department": "...", "influence": "economic_buyer | '
        f'champion | technical_evaluator | end_user"}}]}} with up to {limit} titles, best first. '
        "Stop when the next title would be a weak fit."
    )
    return system, user


def parse_titles(text: str) -> list[dict]:
    """``[{title, reason, department, influence}]`` from the model's answer; [] when unreadable."""
    raw = _FENCE.sub("", (text or "").strip())
    match = re.search(r"[\[{].*[\]}]", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
        items = data.get("titles") if isinstance(data, dict) else data
    except (json.JSONDecodeError, ValueError):
        # An answer cut off at the token limit (measured 2026-11-19: the JSON ended mid-reason, so
        # nothing parsed and the catalogue answered) still holds every title before the cut.
        items = []
        for obj in re.finditer(r"\{[^{}]*\}", raw):
            try:
                items.append(json.loads(obj.group(0)))
            except (json.JSONDecodeError, ValueError):
                continue
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = clean_title(item.get("title"))
        if not title:
            continue
        influence = str(item.get("influence") or "").strip().lower()
        out.append({
            "title": title,
            "reason": re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:240],
            "department": str(item.get("department") or "").strip()[:60],
            "influence": influence if influence in _INFLUENCES else "",
        })
    return out


async def suggest_titles(icp: dict, *, llm=None, limit: int = MAX_SUGGESTIONS,
                         ) -> tuple[list[TitleRecommendation], str]:
    """Up to ``limit`` titles for the ICP, and where they came from (``ai`` or ``catalogue``).
    Never raises."""
    icp = dict(icp or {})
    limit = min(MAX_SUGGESTIONS, max(1, int(limit or MAX_SUGGESTIONS)))
    if llm is not None:
        kept = await _from_model(icp, llm=llm, limit=limit)
        if kept:
            return kept, "ai"
    return from_catalogue(icp, limit=limit), "catalogue"


async def _from_model(icp: dict, *, llm, limit: int) -> list[TitleRecommendation]:
    from nexus.agents.llm import LLMMessage

    system, user = build_prompt(icp, limit=limit)
    try:
        response = await llm.complete(
            [LLMMessage("system", system), LLMMessage("user", user)],
            temperature=0.2, max_tokens=2000, purpose="title_suggestions",
        )
    except Exception as exc:
        logger.info("title suggestions: the model failed, using the catalogue: %r", exc)
        return []
    if getattr(response, "is_stub", False):
        return []
    seen = _listed(icp)
    survivors = [item for item in parse_titles(getattr(response, "text", "") or "")
                 if keep(item["title"], icp, seen)]
    # Budget owners and champions before evaluators and users, the model's order within each.
    survivors.sort(key=lambda item: _INFLUENCE_RANK.get(item["influence"], len(_INFLUENCES)))
    return [
        TitleRecommendation(
            title=item["title"], priority_score=max(40, 95 - 5 * rank), confidence=0.75,
            department=item["department"], buying_influence=item["influence"],
            reason=item["reason"] or "Suggested for this offer.",
        )
        for rank, item in enumerate(survivors[:limit])
    ]


#: The level words a keyword title is built with, most senior first. C-level is left out: "Chief of
#: Facilities" is not a title anyone holds.
_KEYWORD_LEVELS = (("vp", "VP"), ("head", "Head"), ("director", "Director"),
                   ("manager", "Manager"))


def keyword_titles(icp: dict) -> list[TitleRecommendation]:
    """Titles built from the ICP's title keywords at its job levels ("Head of Facilities"), the
    form people actually hold. Without these the fallback ranked a fixed list of roles, none of them
    in the function the SDR typed, which is the "ignores keywords" half of the report."""
    keywords = [str(k).strip() for k in (icp.get("title_keywords") or []) if str(k).strip()]
    levels = {str(x).strip().lower() for x in (icp.get("job_levels") or [])}
    words = [(lv, w) for lv, w in _KEYWORD_LEVELS if lv in levels] or [
        ("head", "Head"), ("director", "Director")]
    out: list[TitleRecommendation] = []
    for keyword in keywords:
        label = keyword if keyword[:1].isupper() else keyword.title()
        for level, word in words:
            out.append(TitleRecommendation(
                title=f"{word} of {label}", priority_score=max(40, 90 - 3 * len(out)),
                confidence=0.6, department=label, buying_influence="",
                reason=f'In "{keyword}", one of your title keywords, at {level} level.',
            ))
    return out


def _in_keywords(rec: TitleRecommendation, keywords: list[str]) -> bool:
    """Is this catalogue role in one of the functions the SDR typed (its title, department or a
    common alternative phrasing)?"""
    text = _normalise(" ".join([rec.title, rec.department, *rec.alternatives]))
    return any(k in text for k in keywords)


def from_catalogue(icp: dict, *, limit: int) -> list[TitleRecommendation]:
    """The deterministic suggestions, through the same filter: titles in the ICP's own title
    keywords first, then the catalogue ranking. With title keywords set, only catalogue roles in
    those functions: a VP Sales beside "infrastructure, estates" is the ignored-keywords report."""
    from nexus.relevance.titles import _context_blob, _context_bonus

    seen = _listed(icp)
    keywords = [_normalise(str(k)) for k in (icp.get("title_keywords") or []) if _normalise(str(k))]
    catalogue = [r for r in ranked_titles_for_icp(icp)
                 if not keywords or _in_keywords(r, keywords)]
    # With the offer described, only roles its own words speak to, when there are any. An ICP with
    # several industries otherwise lifts every industry's operational role: a lead-generation
    # platform targeting Manufacturing was offered "Plant Manager" and "Head of Maintenance"
    # (measured 2026-11-19). Fewer, better titles, as the model is asked for.
    blob = _context_blob(icp)
    if blob:
        spoken = [r for r in catalogue if _context_bonus(r.title, r, blob) > 0]
        catalogue = spoken or catalogue
    out: list[TitleRecommendation] = []
    for rec in [*keyword_titles(icp), *catalogue]:
        if len(out) >= limit:
            break
        if keep(rec.title, icp, seen):
            out.append(rec)
    return out
