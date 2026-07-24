"""Title recommendation engine — which buying-committee roles to target for an account.

Today target titles are STATIC: they come from the tenant's ICP ``buyer_titles`` or, when unset,
a five-role default committee (see ``integrations/contact_search.py``). This module adds an
explainable engine that *recommends* ideal titles per account from firmographics, so contact
sourcing and the UI can target the right buyer instead of a fixed list.

It is deterministic and offline (no LLM, no network) so it is fast, free, and fully testable; an
LLM re-ranker can layer on later behind a flag. It is purely additive — nothing calls it unless a
caller opts in, so existing behaviour is unchanged.

Scoring per role, per account (0..100):
  base_priority
    + size-band fit      (SMB favours founders/owners; enterprise favours C-suite + specialists)
    + industry match     (role elevated in the account's industry)
    + tech-stack match   (e.g. a data warehouse in the stack elevates Head of Data)
    + ICP alignment      (the tenant already targets this title -> strong boost)
Confidence rises with the number of independent factors that fired, so a role matched on size +
industry + ICP is more trustworthy than one matched on size alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Company-size bands (by employee count). Roles declare which bands they fit.
SMB = "smb"          # < 200
MID = "mid"          # 200..2000
ENT = "enterprise"   # > 2000

# Buying-influence roles in a B2B committee (Challenger/MEDDIC vocabulary).
ECONOMIC = "economic_buyer"        # signs the cheque
CHAMPION = "champion"              # drives the deal internally
TECHNICAL = "technical_evaluator"  # vets the solution
USER = "end_user"                  # lives in the product day-to-day


@dataclass(frozen=True)
class RoleTemplate:
    title: str
    department: str
    influence: str
    base_priority: int
    bands: tuple[str, ...]                     # size bands where this role is relevant
    industries: tuple[str, ...] = ()           # industries that elevate this role (lowercased match)
    tech: tuple[str, ...] = ()                 # tech-stack tokens that elevate this role
    alternatives: tuple[str, ...] = ()
    seniority: str = "leader"


# Curated B2B buying-committee knowledge base. Kept compact but spanning every department a GTM
# deal touches; the scoring — not the list — does the per-account tailoring.
ROLES: tuple[RoleTemplate, ...] = (
    # ---- Executive ----
    RoleTemplate("Chief Executive Officer", "Executive", ECONOMIC, 62, (SMB, MID),
                 alternatives=("Founder", "Co-Founder", "President"), seniority="c_level"),
    RoleTemplate("Founder", "Executive", ECONOMIC, 70, (SMB,),
                 alternatives=("Co-Founder", "Owner", "Managing Director"), seniority="c_level"),
    RoleTemplate("Chief Operating Officer", "Executive", ECONOMIC, 60, (MID, ENT),
                 alternatives=("VP Operations", "Head of Operations"), seniority="c_level"),
    # ---- Sales / Revenue ----
    RoleTemplate("Chief Revenue Officer", "Sales", ECONOMIC, 82, (MID, ENT),
                 alternatives=("VP Revenue", "SVP Sales"), seniority="c_level"),
    RoleTemplate("VP Sales", "Sales", ECONOMIC, 80, (SMB, MID, ENT),
                 alternatives=("Head of Sales", "Sales Director", "VP of Sales"), seniority="vp"),
    RoleTemplate("Head of Sales Development", "Sales", CHAMPION, 74, (MID, ENT),
                 alternatives=("Director of SDR", "VP Sales Development"), seniority="director"),
    RoleTemplate("Revenue Operations Leader", "Revenue Operations", TECHNICAL, 78, (MID, ENT),
                 tech=("salesforce", "hubspot", "outreach", "salesloft", "gong"),
                 alternatives=("Head of RevOps", "VP Revenue Operations", "Sales Operations Manager"),
                 seniority="director"),
    # ---- Marketing ----
    RoleTemplate("Chief Marketing Officer", "Marketing", ECONOMIC, 72, (MID, ENT),
                 alternatives=("VP Marketing", "Head of Marketing"), seniority="c_level"),
    RoleTemplate("Head of Demand Generation", "Marketing", CHAMPION, 68, (MID, ENT),
                 tech=("marketo", "hubspot", "segment"),
                 alternatives=("Demand Gen Manager", "Growth Marketing Lead"), seniority="director"),
    # ---- Finance ----
    RoleTemplate("Chief Financial Officer", "Finance", ECONOMIC, 66, (MID, ENT),
                 industries=("financial services", "fintech", "insurance", "banking"),
                 alternatives=("VP Finance", "Finance Director"), seniority="c_level"),
    RoleTemplate("VP Finance", "Finance", CHAMPION, 58, (MID, ENT),
                 alternatives=("Controller", "Head of FP&A"), seniority="vp"),
    # ---- Engineering / Product ----
    RoleTemplate("Chief Technology Officer", "Engineering", TECHNICAL, 70, (SMB, MID, ENT),
                 tech=("aws", "gcp", "azure", "kubernetes"),
                 alternatives=("VP Engineering", "Head of Engineering"), seniority="c_level"),
    RoleTemplate("VP Engineering", "Engineering", TECHNICAL, 64, (MID, ENT),
                 alternatives=("Head of Engineering", "Director of Engineering"), seniority="vp"),
    RoleTemplate("Head of Product", "Product", CHAMPION, 62, (MID, ENT),
                 alternatives=("VP Product", "Chief Product Officer", "Director of Product"),
                 seniority="director"),
    # ---- Data ----
    RoleTemplate("Head of Data", "Data", TECHNICAL, 66, (MID, ENT),
                 tech=("snowflake", "databricks", "looker", "segment", "bigquery", "redshift"),
                 alternatives=("Chief Data Officer", "VP Analytics", "Director of Data"),
                 seniority="director"),
    # ---- IT / Security ----
    RoleTemplate("Chief Information Security Officer", "Security", TECHNICAL, 64, (MID, ENT),
                 industries=("financial services", "fintech", "healthcare", "insurance", "banking"),
                 tech=("okta", "vanta", "crowdstrike", "cloudflare"),
                 alternatives=("Head of Security", "VP Security", "Director of Information Security"),
                 seniority="c_level"),
    RoleTemplate("Chief Information Officer", "IT", ECONOMIC, 60, (ENT,),
                 alternatives=("VP IT", "Head of IT"), seniority="c_level"),
    # ---- Operations / People ----
    RoleTemplate("Head of Operations", "Operations", CHAMPION, 56, (SMB, MID),
                 alternatives=("VP Operations", "Operations Manager"), seniority="director"),
    RoleTemplate("Chief People Officer", "People", ECONOMIC, 50, (MID, ENT),
                 industries=("software", "saas", "technology"),
                 alternatives=("VP People", "Head of People", "CHRO"), seniority="c_level"),
)


def size_band(employee_count: int | None) -> str | None:
    if not employee_count or employee_count <= 0:
        return None
    if employee_count < 200:
        return SMB
    if employee_count <= 2000:
        return MID
    return ENT


@dataclass
class TitleRecommendation:
    title: str
    priority_score: int          # 0..100
    confidence: float            # 0..1
    department: str
    buying_influence: str
    reason: str
    alternatives: list[str] = field(default_factory=list)


def _norm(values) -> set[str]:
    return {str(v).strip().lower() for v in (values or []) if str(v).strip()}


def recommend_titles(
    *,
    industry: str | None = None,
    employee_count: int | None = None,
    tech_stack: list[str] | None = None,
    icp_buyer_titles: list[str] | None = None,
    department: str | None = None,
    limit: int = 8,
) -> list[TitleRecommendation]:
    """Rank buying-committee titles for one account's firmographics. Deterministic and explainable.

    ``icp_buyer_titles`` are the titles the tenant already targets (from their ICP) — a match is a
    strong signal, so those titles are boosted and cited in the reason. ``department`` optionally
    restricts to one function. Never raises; unknown/empty inputs simply yield lower confidence.
    """
    band = size_band(employee_count)
    industry_l = (industry or "").strip().lower()
    tech_l = _norm(tech_stack)
    icp_l = _norm(icp_buyer_titles)
    dept_l = (department or "").strip().lower()

    out: list[TitleRecommendation] = []
    for role in ROLES:
        if dept_l and role.department.lower() != dept_l:
            continue

        score = role.base_priority
        factors: list[str] = []
        matched = 0

        # Size-band fit (or a mild penalty when we know the size and it doesn't fit the role).
        if band is not None:
            if band in role.bands:
                score += 12
                matched += 1
                factors.append(f"fits {band} company size")
            else:
                score -= 10

        # Industry elevation.
        if industry_l and role.industries and any(industry_l == i or i in industry_l
                                                   for i in role.industries):
            score += 14
            matched += 1
            factors.append(f"elevated in {industry}")

        # Tech-stack elevation.
        if tech_l and role.tech:
            hit = sorted(tech_l & set(role.tech))
            if hit:
                score += 12
                matched += 1
                factors.append("tech stack signals " + "/".join(hit[:2]))

        # ICP alignment — the tenant explicitly targets this title (or an alternative).
        role_titles = _norm((role.title, *role.alternatives))
        if icp_l and (icp_l & role_titles or any(t in " ".join(icp_l) for t in role_titles)):
            score += 22
            matched += 1
            factors.append("matches your ICP buyer titles")

        score = max(0, min(100, score))
        # Confidence: 0.4 floor, +0.15 per independent factor that fired (capped at 1.0).
        confidence = round(min(1.0, 0.4 + 0.15 * matched), 2)
        reason = (
            f"{role.title} — {role.influence.replace('_', ' ')}; " + "; ".join(factors)
            if factors else
            f"{role.title} — {role.influence.replace('_', ' ')} on a typical B2B buying committee"
        )
        out.append(
            TitleRecommendation(
                title=role.title,
                priority_score=score,
                confidence=confidence,
                department=role.department,
                buying_influence=role.influence,
                reason=reason,
                alternatives=list(role.alternatives),
            )
        )

    # Highest priority first; stable tie-break by confidence then title for determinism.
    out.sort(key=lambda r: (-r.priority_score, -r.confidence, r.title))
    return out[: max(1, limit)]


def recommend_titles_for_icp(icp: dict, *, limit: int = 10) -> list[TitleRecommendation]:
    """Recommend up to ``limit`` (capped at 10) buyer titles for a whole ICP, not one account.

    Considers ALL of the ICP's fields: every target industry (a title elevated in *any* of them
    wins that boost), the midpoint of the employee range, the required tech stack, and the titles
    already listed. Deterministic; safe on a partial/empty ICP (falls back to the base ranking).
    """
    icp = icp or {}
    industries = [i for i in (icp.get("industries") or []) if str(i).strip()] or [None]
    emin, emax = icp.get("employee_min"), icp.get("employee_max")
    if emin and emax:
        employee_count: int | None = (int(emin) + int(emax)) // 2
    else:
        employee_count = emin or emax
    tech = icp.get("required_tech") or []
    buyer_titles = icp.get("buyer_titles") or icp.get("titles") or []

    # Score against each target industry; keep the best score per title across industries.
    best: dict[str, TitleRecommendation] = {}
    for industry in industries:
        for rec in recommend_titles(
            industry=industry,
            employee_count=employee_count,
            tech_stack=tech,
            icp_buyer_titles=buyer_titles,
            limit=len(ROLES),
        ):
            current = best.get(rec.title)
            if current is None or rec.priority_score > current.priority_score:
                best[rec.title] = rec
    out = sorted(best.values(), key=lambda r: (-r.priority_score, -r.confidence, r.title))
    return out[: min(10, max(1, limit))]
