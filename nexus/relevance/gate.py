"""Mandatory ICP fields: a hard gate before any score (SDR feedback, 2026-10-26).

Reported: accounts scored 60-70% fit while failing Industry and Tech Stack, the campaign's main
criteria. `engine.score_icp_fit` is a weighted mean, so a miss on the field that matters most is
outvoted by the others (industry 0, size 1, geo 1, tech unknown 0.5, revenue 1 is 65).

Decided with the owner (D3, D3a, D3b):

* Industry, Company size, Country, Region, Tech stack and Job titles can each be **mandatory**
  (``icp["mandatory"]``); a mandatory field must match, whatever the score.
* **Exact means the same meaning**, not the same spelling: a child industry code counts for its
  parent, "VP Sales" is "Vice President of Sales", "HubSpot CRM" is HubSpot, USA is the United
  States. Never a loose keyword overlap.
* **No data on a mandatory field is not a match.** It is reported as unknown, so a screen can say
  "no data for Tech stack" rather than "fails".
* **Country is the headquarters**: ``Account.country`` is the HQ on every write path (LinkedIn's
  HQ office, enrichment's "where the company is headquartered").

One function, read by every path that decides who is in the ICP: delivery (every source, LinkedIn
included), the Accounts list, list building and campaign enrolment. Deterministic, no LLM, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FIELDS = ("industry", "size", "country", "region", "tech", "titles")
LABELS = {"industry": "Industry", "size": "Company size", "country": "Country",
          "region": "State / province", "tech": "Tech stack", "titles": "Job titles"}


@dataclass(frozen=True)
class GateResult:
    passed: bool = True
    failed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        parts = []
        if self.failed:
            parts.append("Fails " + ", ".join(LABELS[f] for f in self.failed))
        if self.unknown:
            parts.append("no data for " + ", ".join(LABELS[f] for f in self.unknown))
        text = "; ".join(parts)
        return text[:1].upper() + text[1:]

    def as_dict(self) -> dict:
        return {"passed": self.passed, "failed": list(self.failed), "unknown": list(self.unknown),
                "reason": self.reason}


def mandatory_fields(icp: dict | None) -> list[str]:
    values = (icp or {}).get("mandatory") or []
    return [f for f in FIELDS if f in {str(v) for v in values}]


# ---- industry ----------------------------------------------------------------------------------

def _icp_codes(icp: dict) -> list[int]:
    """The ICP's LinkedIn codes: stored ones from the current mapping, else mapped now, without the
    LLM (a gate must not make network calls)."""
    from nexus.prospecting.industries import MAPPING_VERSION, match_term, split_terms

    stored = [int(c) for c in (icp.get("linkedin_industry_ids") or []) if str(c).isdigit()]
    if stored and icp.get("linkedin_industry_map_version") in (None, MAPPING_VERSION):
        return stored
    codes: list[int] = []
    for term in split_terms(icp.get("industries") or []):
        codes.extend(c for c in match_term(term) if c not in codes)
    return codes or stored


def _industry_matches(text: str | None, icp: dict, codes: list[int]) -> bool:
    from nexus.prospecting.industries import _norm, descendants, match_term

    if not text:
        return False
    wanted = {_norm(i) for i in icp.get("industries") or []}
    if _norm(text) in wanted:
        return True
    return bool(codes) and bool(set(match_term(text)) & descendants(codes))


def _industry(icp: dict, account) -> str | None:
    if not icp.get("industries"):
        return None
    if not account.industry:
        return "unknown"
    codes = _icp_codes(icp)
    if _industry_matches(account.industry, icp, codes):
        return "ok"
    custom = account.custom_fields if isinstance(account.custom_fields, dict) else {}
    if icp.get("include_suppliers") and _industry_matches(custom.get("industry_self_label"), icp,
                                                          codes):
        return "ok"
    return "fail"


# ---- size, country, region -------------------------------------------------------------------

def _size(icp: dict, account) -> str | None:
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if lo is None and hi is None:
        return None
    count = account.employee_count
    if count is None:
        # A checked band (`custom_fields.employee_band`, 2026-10-29) answers without a number:
        # inside the ICP's range is a match, wholly outside it a miss, straddling it unknown.
        custom = account.custom_fields if isinstance(account.custom_fields, dict) else {}
        band = custom.get("employee_band")
        if isinstance(band, dict):
            bmin, bmax = band.get("min"), band.get("max")
            inside = ((lo is None or (bmin is not None and bmin >= lo))
                      and (hi is None or (bmax is not None and bmax <= hi)))
            outside = ((lo is not None and bmax is not None and bmax < lo)
                       or (hi is not None and bmin is not None and bmin > hi))
            if inside:
                return "ok"
            if outside:
                return "fail"
        return "unknown"
    if (lo is not None and count < lo) or (hi is not None and count > hi):
        return "fail"
    return "ok"


def _country(icp: dict, account) -> str | None:
    from nexus.discovery.auto import _norm_country

    wanted = {_norm_country(c) for c in icp.get("countries") or [] if str(c).strip()}
    if not wanted:
        return None
    if not (account.country or "").strip():
        return "unknown"
    return "ok" if _norm_country(account.country) in wanted else "fail"


def _region(icp: dict, account) -> str | None:
    wanted = {str(r).strip().lower() for r in icp.get("regions") or [] if str(r).strip()}
    if not wanted:
        return None
    custom = account.custom_fields if isinstance(account.custom_fields, dict) else {}
    region = (account.region or custom.get("region") or "").strip().lower()
    if not region:
        return "unknown"
    return "ok" if region in wanted else "fail"


# ---- tech --------------------------------------------------------------------------------------

#: Words a product name carries that are not the product: "HubSpot CRM", "Salesforce Sales Cloud".
_SUFFIXES = ("sales cloud", "service cloud", "marketing cloud", "commerce cloud", "marketing hub",
             "sales hub", "service hub", "cms hub", "crm", "inc", "cloud", "platform", "software",
             "suite", "engage", "online")
_ALIASES = {
    "sfdc": "salesforce", "salesforce com": "salesforce",
    "dynamics 365": "microsoft dynamics", "microsoft dynamics 365": "microsoft dynamics",
    "ms dynamics": "microsoft dynamics", "dynamics": "microsoft dynamics",
    "office 365": "microsoft 365", "o365": "microsoft 365",
    "g suite": "google workspace", "gsuite": "google workspace",
    "outreach io": "outreach", "adobe marketo": "marketo",
    "salesforce pardot": "pardot", "marketing cloud account engagement": "pardot",
    "zoominfo": "zoominfo", "zoom info": "zoominfo", "sales navigator": "linkedin sales navigator",
}


def canonical_product(name: str) -> str:
    """One spelling per product: "HubSpot CRM", "hubspot" and "HubSpot Marketing Hub" agree."""
    text = re.sub(r"\.(com|io|ai|co)\b", "", (name or "").lower())
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())
    changed = True
    while changed and text:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(" " + suffix) and len(text) > len(suffix) + 1:
                text = text[: -len(suffix) - 1].strip()
                changed = True
    return _ALIASES.get(text, text)


def tech_terms(entry: str) -> list[str]:
    """The products one ICP tech entry names. A group names its products: "CRM (Salesforce,
    HubSpot, Zoho)" is any of three, and so are the fragments the old comma split left behind
    ("CRM (Salesforce", "Zoho)")."""
    text = str(entry or "").strip()
    if "(" in text:
        text = text.split("(", 1)[1]
    text = text.rstrip(")").strip()
    parts = re.split(r"\s*(?:,|/|;|\||\bor\b)\s*", text)
    out: list[str] = []
    for part in parts:
        product = canonical_product(part)
        if product and product not in out:
            out.append(product)
    return out


def _tech(icp: dict, account) -> str | None:
    entries = [t for t in icp.get("required_tech") or [] if str(t).strip()]
    if not entries:
        return None
    owned = {canonical_product(t) for t in account.tech_stack or [] if str(t).strip()}
    if not owned:
        return "unknown"
    wanted = [tech_terms(e) for e in entries]
    if (icp.get("tech_match") or "any") == "all":
        ok = all(any(p in owned for p in group) for group in wanted if group)
    else:
        ok = any(p in owned for group in wanted for p in group)
    return "ok" if ok else "fail"


# ---- the gate ----------------------------------------------------------------------------------

_CHECKS = {"industry": _industry, "size": _size, "country": _country, "region": _region,
           "tech": _tech}


def icp_gate(icp: dict | None, account) -> GateResult:
    """Does ``account`` meet every mandatory field of ``icp``? Job titles are a contact's field,
    checked where people are found (`lookalike.contacts.title_fits`), never on an account."""
    icp = icp or {}
    failed: list[str] = []
    unknown: list[str] = []
    for name in mandatory_fields(icp):
        check = _CHECKS.get(name)
        if check is None:
            continue
        outcome = check(icp, account)
        if outcome == "fail":
            failed.append(name)
        elif outcome == "unknown":
            unknown.append(name)
    return GateResult(passed=not failed and not unknown, failed=failed, unknown=unknown)


# ---- titles: the same meaning ------------------------------------------------------------------

_TITLE_STOP = frozenset({"of", "the", "and", "for", "in", "at", "a", "an", "to"})


def _title_tokens(title: str) -> frozenset[str]:
    from nexus.lookalike.contacts import expand_title

    words = re.split(r"[^a-z0-9]+", expand_title(title or ""))
    return frozenset(w for w in words if w and w not in _TITLE_STOP)


def same_meaning_title(title: str, wanted) -> bool:
    """Is ``title`` one of ``wanted``, in any word order, abbreviation or with qualifiers added
    ("Senior Operations Manager, EMEA" is an Operations Manager)? Every word of a wanted title must
    be in it, so "Lead Operator" is not an Operations Manager and "Manager" alone is nobody's."""
    have = _title_tokens(title)
    if not have:
        return False
    return any(want and want <= have for want in (_title_tokens(w) for w in wanted or []))
