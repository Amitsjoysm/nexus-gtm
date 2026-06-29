"""Seed↔candidate similarity scoring for the look-alike finders.

The old look-alike score measured *ICP fit of an empty candidate* — degenerate and uniform. This
module scores how closely a candidate **resembles the seed** across the dimensions a rep cares
about: industry, sub-industry/niche, geography (graded city > state/region > country), employee
size, revenue, technologies, and focus/SEO keywords.

Design principles:
  * **Fuzzy, not strict** — partial credit everywhere (token overlap, log-proximity, graded geo).
  * **Never penalize missing data** — a dimension is scored only when both sides have it; weights
    are re-normalized over the dimensions actually present, so sparse candidates aren't punished.
  * **Pure + deterministic** — no I/O, no LLM; trivially unit-testable.

Performance: the scorers are called in a tight loop (one seed vs. N candidates). To avoid
re-tokenizing the *seed* N times, feature extraction is split out into :func:`prepare_company` /
:func:`prepare_contact`, and the public ``*_similarity`` functions accept a precomputed
``seed_features`` so a caller can extract the seed once and reuse it across the whole pool. The
zero-argument-extra call path is unchanged and produces identical scores.

All firmographics live on the :class:`~nexus.models.account.Account` columns
(industry/employee_count/country/tech_stack) plus ``custom_fields`` (description/keywords/
sub_industry/niche/revenue/region/city), so the same function works for persisted and transient
(candidate) accounts.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class SimilarityResult:
    score: int  # 0..100
    reasons: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)


# Default dimension weights (renormalized over whichever dimensions are present). Tunable.
COMPANY_WEIGHTS: dict[str, float] = {
    "industry": 0.20,
    "sub_industry": 0.12,
    "geo": 0.15,
    "size": 0.15,
    "revenue": 0.10,
    "tech": 0.13,
    "keywords": 0.15,
}

CONTACT_WEIGHTS: dict[str, float] = {
    "title": 0.45,
    "seniority": 0.20,
    "department": 0.15,
    "company": 0.20,
}

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "inc", "llc", "ltd", "corp", "company", "co", "group",
    "solutions", "services", "service", "platform", "software", "technologies", "technology",
    "global", "international", "systems", "labs", "the", "a", "an", "of", "to", "in", "is",
    "we", "our", "your", "their", "based", "leading", "best", "top", "online", "digital",
})
_WORD_RE = re.compile(r"[a-z0-9]+")


def _cf(acc) -> dict:
    return getattr(acc, "custom_fields", None) or {}


def _tokens(*texts) -> set[str]:
    """Lowercase content tokens (len ≥ 3, stopwords dropped) from strings and/or string lists."""
    out: set[str] = set()
    for t in texts:
        if not t:
            continue
        if isinstance(t, (list, tuple, set)):
            t = " ".join(str(x) for x in t)
        for w in _WORD_RE.findall(str(t).lower()):
            if len(w) >= 3 and w not in _STOPWORDS:
                out.add(w)
    return out


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _log_proximity(a, b, *, tol_factor: float) -> float | None:
    """1.0 when equal, decaying to 0 as the ratio approaches ``tol_factor`` (e.g. 20× apart → 0).
    ``None`` when either value is missing/non-positive (so the caller can skip the dimension)."""
    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return None
    if a <= 0 or b <= 0:
        return None
    ratio = abs(math.log(a) - math.log(b))
    span = math.log(tol_factor)
    return max(0.0, 1.0 - ratio / span) if span > 0 else None


_REV_RE = re.compile(r"([\d][\d.]*)\s*(b|bn|billion|m|mm|million|k|thousand)?", re.IGNORECASE)
_REV_MULT = {"b": 1e9, "bn": 1e9, "billion": 1e9, "m": 1e6, "mm": 1e6, "million": 1e6,
             "k": 1e3, "thousand": 1e3}


def parse_revenue(v) -> float | None:
    """Parse a revenue value into a dollar number. Handles '$10M', '10-50M' (→ midpoint),
    '1.2B', '$10 million', and plain integers. Returns None when unparseable."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v).lower().replace(",", "").replace("$", "").strip()
    if not s:
        return None
    matches = _REV_RE.findall(s)
    nums: list[tuple[float, str]] = []
    for numtxt, unit in matches:
        try:
            nums.append((float(numtxt), unit))
        except ValueError:
            continue
    if not nums:
        return None
    # A range like "10-50m" leaves the first number unit-less; inherit the last explicit unit.
    shared = next((u for _, u in reversed(nums) if u), "")
    vals = [n * _REV_MULT.get(u or shared, 1.0) for n, u in nums]
    avg = sum(vals) / len(vals)
    return avg if avg > 0 else None


def _keyword_set(acc) -> set[str]:
    cf = _cf(acc)
    return _tokens(getattr(acc, "name", ""), getattr(acc, "industry", ""),
                   cf.get("description"), cf.get("keywords"), cf.get("sub_industry"), cf.get("niche"))


# ---- company features (extracted once, scored many times) -----------------------------------
@dataclass(slots=True)
class CompanyFeatures:
    industry: str
    industry_l: str
    industry_tokens: set[str]
    niche_tokens: set[str]
    sub_industry: str
    country_disp: str | None
    country_l: str
    region_disp: object
    region_l: str
    city_disp: object
    city_l: str
    employee_count: object
    revenue_val: float | None
    revenue_disp: object
    tech: set[str]
    keywords: set[str]


def prepare_company(acc) -> CompanyFeatures:
    """Extract a company's scoring features once (the expensive part: tokenization). Reuse across
    a whole candidate pool by passing the result as ``seed_features`` to :func:`company_similarity`."""
    cf = _cf(acc)
    industry = (getattr(acc, "industry", "") or "").strip()
    country = (getattr(acc, "country", "") or "").strip()
    region = str(cf.get("region") or "").strip()
    city = str(cf.get("city") or "").strip()
    return CompanyFeatures(
        industry=industry,
        industry_l=industry.lower(),
        industry_tokens=_tokens(industry),
        niche_tokens=_tokens(cf.get("sub_industry"), cf.get("niche")),
        sub_industry=(cf.get("sub_industry") or "").strip(),
        country_disp=getattr(acc, "country", None),
        country_l=country.lower(),
        region_disp=cf.get("region"),
        region_l=region.lower(),
        city_disp=cf.get("city"),
        city_l=city.lower(),
        employee_count=getattr(acc, "employee_count", None),
        revenue_val=parse_revenue(cf.get("revenue")),
        revenue_disp=cf.get("revenue"),
        tech={str(t).lower() for t in (getattr(acc, "tech_stack", None) or [])},
        keywords=_keyword_set(acc),
    )


def _geo_grade(s: CompanyFeatures, c: CompanyFeatures) -> float | None:
    """Graded geo proximity: same city 1.0 > same state/region 0.75 > same country 0.45 >
    different country 0.0. ``None`` when one side has no geo at all (skip the dimension)."""
    if not (s.country_l or s.region_l or s.city_l) or not (c.country_l or c.region_l or c.city_l):
        return None
    if s.city_l and c.city_l and s.city_l == c.city_l:
        return 1.0
    if s.region_l and c.region_l and s.region_l == c.region_l:
        return 0.75
    if s.country_l and c.country_l:
        return 0.45 if s.country_l == c.country_l else 0.0
    return None  # only one side has a country — can't compare reliably


def _score_company(s: CompanyFeatures, c: CompanyFeatures, w: dict[str, float]) -> SimilarityResult:
    dims: dict[str, float] = {}
    reasons: list[str] = []

    # --- industry (exact, else token overlap) ---
    if s.industry and c.industry:
        if s.industry_l == c.industry_l:
            dims["industry"] = 1.0
            reasons.append(f"Same industry: {c.industry}")
        else:
            j = _jaccard(s.industry_tokens, c.industry_tokens)
            dims["industry"] = j
            if j >= 0.34:
                reasons.append(f"Related industry: {c.industry}")

    # --- sub-industry / niche (token overlap) ---
    if s.niche_tokens and c.niche_tokens:
        j = _jaccard(s.niche_tokens, c.niche_tokens)
        dims["sub_industry"] = j
        if j >= 0.34 and c.sub_industry:
            reasons.append(f"Similar niche: {c.sub_industry}")

    # --- geography (graded) ---
    geo = _geo_grade(s, c)
    if geo is not None:
        dims["geo"] = geo
        if geo >= 1.0 and c.city_disp:
            reasons.append(f"Same city: {c.city_disp}")
        elif geo >= 0.75 and c.region_disp:
            reasons.append(f"Same region: {c.region_disp}")
        elif geo >= 0.45 and c.country_disp:
            reasons.append(f"Same country: {c.country_disp}")

    # --- employee size (log-proximity; 20× apart → 0) ---
    size = _log_proximity(s.employee_count, c.employee_count, tol_factor=20)
    if size is not None:
        dims["size"] = size
        if size >= 0.6:
            reasons.append(f"Similar size (~{s.employee_count:,} vs ~{c.employee_count:,} employees)")

    # --- revenue (log-proximity; 50× apart → 0) ---
    rev = _log_proximity(s.revenue_val, c.revenue_val, tol_factor=50)
    if rev is not None:
        dims["revenue"] = rev
        if rev >= 0.6 and c.revenue_disp:
            reasons.append(f"Similar revenue: {c.revenue_disp}")

    # --- technology stack (Jaccard) ---
    if s.tech and c.tech:
        dims["tech"] = _jaccard(s.tech, c.tech)
        shared = sorted(s.tech & c.tech)
        if shared:
            reasons.append("Shared tech: " + ", ".join(shared[:4]))

    # --- keywords / SEO / focus (almost always available — name + description + keywords) ---
    if s.keywords and c.keywords:
        j = _jaccard(s.keywords, c.keywords)
        dims["keywords"] = j
        overlap = sorted(s.keywords & c.keywords)
        if j >= 0.15 and overlap:
            reasons.append("Overlapping focus: " + ", ".join(overlap[:4]))

    if not dims:
        return SimilarityResult(score=50, reasons=["Too little data to compare; neutral"], breakdown={})
    total_w = sum(w.get(k, 0.0) for k in dims) or 1.0
    score = round(sum(dims[k] * w.get(k, 0.0) for k in dims) / total_w * 100)
    return SimilarityResult(score=score, reasons=reasons or ["Scored on available firmographics"],
                            breakdown=dims)


def company_similarity(
    seed, candidate, *, weights: dict[str, float] | None = None,
    seed_features: CompanyFeatures | None = None,
) -> SimilarityResult:
    """Score 0..100 how closely ``candidate`` resembles ``seed`` (the higher, the closer).

    Pass ``seed_features`` (from :func:`prepare_company`) to skip re-extracting the seed in a loop.
    """
    s = seed_features if seed_features is not None else prepare_company(seed)
    return _score_company(s, prepare_company(candidate), weights or COMPANY_WEIGHTS)


# ---- Contact-level similarity ---------------------------------------------------------------
_SENIORITY_RANK: dict[str, int] = {
    "chief": 5, "cxo": 5, "ceo": 5, "cto": 5, "cfo": 5, "coo": 5, "cmo": 5, "cro": 5,
    "founder": 5, "owner": 5, "president": 5, "c-level": 5, "c level": 5,
    "vp": 4, "vice president": 4, "svp": 4, "evp": 4,
    "head": 3, "director": 3,
    "senior": 2, "lead": 2, "manager": 2, "principal": 2,
    "associate": 1, "specialist": 1, "rep": 1, "analyst": 1, "coordinator": 1, "junior": 1,
}
_DEPARTMENTS: dict[str, frozenset[str]] = {
    "executive": frozenset({"ceo", "founder", "owner", "president", "chief executive"}),
    "sales": frozenset({"sales", "revenue", "account executive", "bdr", "sdr", "business development"}),
    "marketing": frozenset({"marketing", "growth", "demand", "brand", "content", "cmo"}),
    "engineering": frozenset({"engineer", "developer", "software", "devops", "infrastructure",
                              "platform", "data", "technical", "cto", "architect"}),
    "finance": frozenset({"finance", "financial", "accounting", "cfo", "controller", "treasur"}),
    "operations": frozenset({"operations", "ops", "coo", "supply"}),
    "product": frozenset({"product", "design", "ux", "ui"}),
    "people": frozenset({"people", "talent", "recruit", "human resources", "hr"}),
}


def _seniority_rank(seniority: str | None, title: str | None) -> int | None:
    hay = f"{seniority or ''} {title or ''}".lower()
    best = 0
    for key, rank in _SENIORITY_RANK.items():
        if key in hay:
            best = max(best, rank)
    return best or None


def _department(title: str | None) -> str | None:
    t = (title or "").lower()
    for dept, kws in _DEPARTMENTS.items():
        if any(k in t for k in kws):
            return dept
    return None


@dataclass(slots=True)
class ContactFeatures:
    title: str
    title_l: str
    title_tokens: set[str]
    rank: int | None
    dept: str | None


def prepare_contact(contact) -> ContactFeatures:
    """Extract a contact's scoring features once. Reuse across a pool via ``seed_features``."""
    title = (getattr(contact, "title", "") or "").strip()
    return ContactFeatures(
        title=title,
        title_l=title.lower(),
        title_tokens=_tokens(title),
        rank=_seniority_rank(getattr(contact, "seniority", None), title),
        dept=_department(title),
    )


def _score_contact(s: ContactFeatures, c: ContactFeatures, company_sim: float | None) -> SimilarityResult:
    w = CONTACT_WEIGHTS
    dims: dict[str, float] = {}
    reasons: list[str] = []

    if s.title and c.title:
        if s.title_l == c.title_l:
            dims["title"] = 1.0
            reasons.append(f"Same title: {c.title}")
        else:
            j = _jaccard(s.title_tokens, c.title_tokens)
            dims["title"] = j
            if j >= 0.34:
                reasons.append(f"Similar role: {c.title}")

    if s.rank is not None and c.rank is not None:
        sen = max(0.0, 1.0 - abs(s.rank - c.rank) / 4.0)
        dims["seniority"] = sen
        if sen >= 0.75:
            reasons.append("Similar seniority")

    if s.dept and c.dept:
        dims["department"] = 1.0 if s.dept == c.dept else 0.0
        if s.dept == c.dept:
            reasons.append(f"Same function: {c.dept}")

    if company_sim is not None:
        dims["company"] = max(0.0, min(1.0, company_sim))
        if company_sim >= 0.6:
            reasons.append("At a similar company")

    if not dims:
        return SimilarityResult(score=50, reasons=["Too little data to compare; neutral"], breakdown={})
    total_w = sum(w.get(k, 0.0) for k in dims) or 1.0
    score = round(sum(dims[k] * w.get(k, 0.0) for k in dims) / total_w * 100)
    return SimilarityResult(score=score, reasons=reasons or ["Scored on available attributes"],
                            breakdown=dims)


def contact_similarity(
    seed, candidate, *, company_sim: float | None = None,
    seed_features: ContactFeatures | None = None,
) -> SimilarityResult:
    """Score 0..100 how closely a candidate *person* resembles the seed contact — primarily role
    (title) + seniority + department, plus how similar their company is (``company_sim`` 0..1).

    Pass ``seed_features`` (from :func:`prepare_contact`) to skip re-extracting the seed in a loop.
    """
    s = seed_features if seed_features is not None else prepare_contact(seed)
    return _score_contact(s, prepare_contact(candidate), company_sim)
