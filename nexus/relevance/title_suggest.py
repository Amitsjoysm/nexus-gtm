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
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


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


def keep(title: str, icp: dict, seen: set[str]) -> bool:
    """Does ``title`` survive the filter? ``seen`` holds the normalised titles already listed or
    already kept, and is added to."""
    norm = _normalise(title)
    if not norm or norm in seen:
        return False
    excluded = [x for x in (icp.get("exclude_title_keywords") or []) if str(x).strip()]
    if excluded and not matches_title(title, {"exclude_title_keywords": excluded}):
        return False
    if level_of(title) == "c_level" and not c_level_allowed(icp):
        return False
    seen.add(norm)
    return True


def _listed(icp: dict) -> set[str]:
    return {_normalise(t) for t in (icp.get("buyer_titles") or icp.get("titles") or [])
            if _normalise(str(t))}


def _bullets(values) -> str:
    return ", ".join(str(v).strip() for v in (values or []) if str(v).strip())


def build_prompt(icp: dict, *, limit: int) -> tuple[str, str]:
    """``(system, user)`` for the model. Every field the ICP holds is in it."""
    system = (
        "You are a B2B sales strategist who knows how buying committees work. Suggest the job "
        "titles of the people who would BUY, CHAMPION or EVALUATE this specific offer at the "
        "companies described: the people who feel the pains it solves. Be specific to the offer, "
        "never a generic list of executives. Output ONLY a JSON object, no prose, no code fences."
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
        + f'\n\nReturn {{"titles": [{{"title": "...", "reason": "one line on why this person '
        f'buys or champions THIS offer", "department": "...", "influence": "economic_buyer | '
        f'champion | technical_evaluator | end_user"}}]}} with up to {limit} titles, best first.'
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
    except (json.JSONDecodeError, ValueError):
        return []
    items = data.get("titles") if isinstance(data, dict) else data
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:120]
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
            temperature=0.2, max_tokens=1200, purpose="title_suggestions",
        )
    except Exception as exc:
        logger.info("title suggestions: the model failed, using the catalogue: %r", exc)
        return []
    if getattr(response, "is_stub", False):
        return []
    seen = _listed(icp)
    kept: list[TitleRecommendation] = []
    for rank, item in enumerate(parse_titles(getattr(response, "text", "") or "")):
        if len(kept) >= limit:
            break
        if not keep(item["title"], icp, seen):
            continue
        kept.append(TitleRecommendation(
            title=item["title"], priority_score=max(40, 95 - 5 * rank), confidence=0.75,
            department=item["department"], buying_influence=item["influence"],
            reason=item["reason"] or "Suggested for this offer.",
        ))
    return kept


def from_catalogue(icp: dict, *, limit: int) -> list[TitleRecommendation]:
    """The deterministic ranking, through the same filter."""
    seen = _listed(icp)
    out: list[TitleRecommendation] = []
    for rec in ranked_titles_for_icp(icp):
        if len(out) >= limit:
            break
        if keep(rec.title, icp, seen):
            out.append(rec)
    return out
