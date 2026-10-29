"""New ICP companies from Parallel: Entity Search finds, one Decisions request per company checks.

Owner decisions 2026-10-29 (`docs/superpowers/specs/2026-10-29-parallel-provider-design.md`): the
chain is the shared store, LinkedIn, then this step, then Exa as the fallback; a company found here
is charged exactly as one Exa found (``discovery.account_added``), and what each call cost us is
recorded on ``search.entity`` / ``verify.decision``, never charged.

Every call counts:

* **One search asks for the 100 results its price includes** (Entity Search is priced per request),
  with an objective holding only the ICP's core criterion, industry and country, as its docs advise.
* **Free gates before any paid check**: a company website of its own (`linkedin.company_domain`),
  not held, not taken, not an association, a domain that is the company's (`vetting.vet`).
* **Every vetted survivor is kept in the shared store** (source ``parallel``, its own description),
  checked or not, so the next workspace takes it from the store without anyone paying again.
* **One Decisions request per company carries every check** (industry by what the company itself
  does, HQ country, size band, whether the website is its own), asked a batch at a time and only
  until the request is met. A verdict is cached 90 days by domain and question set.

A company is delivered when its website is its own, nothing answered falls outside the ICP, and
every MUST-MATCH field answered inside it (no data is no match, D3). A choice under 50% is read as
unknown: ranked evidence below even odds is not an answer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace

logger = logging.getLogger("nexus.prospecting.parallel_companies")

SEARCH_CAPABILITY = "search.entity"
DECISION_CAPABILITY = "verify.decision"
#: Decisions asked at once. A batch is the shortfall, never more, so nothing is checked in vain.
MAX_CONCURRENT_CHECKS = 8
MIN_CONFIDENCE = 0.5
#: Statuses that stop the step: asking again would only repeat the refusal.
_STOPPING = frozenset({"not_configured", "rejected", "out_of_credit"})


@dataclass
class ParallelStep:
    candidates: list = field(default_factory=list)
    discarded: Counter = field(default_factory=Counter)
    #: Why this step did not deliver everything asked: not_configured | out_of_credit | rejected |
    #: failed | nothing_new | no_icp. Empty when it delivered.
    note: str = ""
    #: Calls bought (not served from the cache), for the cost record.
    searches: int = 0
    decisions: int = 0


# ---- what is asked -----------------------------------------------------------------------------

def _clean_list(values) -> list[str]:
    return [str(v).strip() for v in values or [] if str(v).strip()]


def objective_for(icp: dict) -> str:
    """The ICP's core criterion in words: industries, then countries. Size and the rest are checked
    afterwards, because a narrow objective loses relevance down a ranked list."""
    industries = _clean_list(icp.get("industries"))
    if not industries:
        return ""
    text = "Companies in " + ", ".join(industries)
    countries = _clean_list(icp.get("countries"))
    if countries:
        text += " headquartered in " + ", ".join(countries)
    return text


def questions_for(icp: dict) -> dict:
    """One Decisions request's questions for a company. Choices carry ``unknown`` so missing
    evidence is said, never guessed."""
    industries = _clean_list(icp.get("industries"))
    joined = ", ".join(industries)
    questions: dict = {
        "identity": {
            "type": "choice",
            "instructions": "Is the website given in the state this company's own website, "
                            "operated by the company itself?",
            "criteria": {
                "own_site": "The website is this company's own site.",
                "parent_site": "The website belongs to a parent, owner or acquirer, not to this "
                               "company itself.",
                "acquired": "This company was acquired or merged and no longer operates under "
                            "its own name.",
                "unknown": "The evidence does not establish whose website it is.",
            },
        },
        "industry": {
            "type": "choice",
            "instructions": "Judged by what the company itself does (its own products, services "
                            "and operations), which industry is it in?",
            "criteria": {
                **{f"ind_{i}": f"Its own business is {label}: it operates in {label} itself."
                   for i, label in enumerate(industries)},
                "supplier": f"It mainly sells products or services to companies in {joined}, "
                            "rather than operating in it.",
                "other": "Its own business is in another industry.",
                "unknown": "The evidence does not establish what the company does.",
            },
        },
    }
    countries = _clean_list(icp.get("countries"))
    if countries:
        questions["hq_country"] = {
            "type": "choice",
            "instructions": "In which country are the company's headquarters?",
            "criteria": {
                **{f"c_{i}": f"Its headquarters are in {c}." for i, c in enumerate(countries)},
                "other": "Its headquarters are in another country.",
                "unknown": "The evidence does not establish where it is headquartered.",
            },
        }
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if lo is not None or hi is not None:
        band = (f"between {lo:,} and {hi:,}" if lo is not None and hi is not None
                else f"at least {lo:,}" if lo is not None else f"at most {hi:,}")
        criteria = {"within": f"It has {band} employees."}
        if lo is not None:
            criteria["below"] = f"It has fewer than {lo:,} employees."
        if hi is not None:
            criteria["above"] = f"It has more than {hi:,} employees."
        criteria["unknown"] = "The evidence does not establish how many employees it has."
        questions["size_band"] = {"type": "choice",
                                  "instructions": "How many employees does the company have?",
                                  "criteria": criteria}
    return questions


def _question_hash(questions: dict) -> str:
    return hashlib.sha1(json.dumps(questions, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ---- what is concluded ---------------------------------------------------------------------------

def _picked(answer) -> str:
    """The chosen option, or ``unknown`` for a refusal, a missing answer or a choice under even
    odds."""
    if answer is None or getattr(answer, "type", "") != "choice" or not answer.choice:
        return "unknown"
    prob = (answer.probabilities or {}).get(answer.choice)
    if prob is not None and float(prob) < MIN_CONFIDENCE:
        return "unknown"
    return answer.choice


def judge(answers: dict, icp: dict) -> tuple[str, dict]:
    """``("", fields)`` to deliver, else ``(reason, {})``. ``fields`` holds the industry and
    country labels and the employee band the answers support."""
    from nexus.prospecting.companies import country_label
    from nexus.relevance.gate import mandatory_fields

    mandatory = set(mandatory_fields(icp))
    if _picked(answers.get("identity")) != "own_site":
        return "not_its_own", {}
    industries = _clean_list(icp.get("industries"))
    countries = _clean_list(icp.get("countries"))
    fields: dict = {}
    unknown: set[str] = set()

    industry = _picked(answers.get("industry"))
    if industry.startswith("ind_") and industry[4:].isdigit() and int(industry[4:]) < len(industries):
        fields["industry"] = industries[int(industry[4:])]
    elif industry == "supplier" and icp.get("include_suppliers"):
        fields["industry"] = industries[0]
    elif industry == "unknown":
        unknown.add("industry")
    else:
        return "wrong_industry", {}

    if "hq_country" in answers or countries:
        country = _picked(answers.get("hq_country"))
        if country.startswith("c_") and country[2:].isdigit() and int(country[2:]) < len(countries):
            from nexus.contacts.phone import region_for

            label = countries[int(country[2:])]
            fields["country"] = country_label(region_for(label)) or label
        elif country == "unknown":
            unknown.add("country")
        else:
            return "outside_icp", {}

    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if lo is not None or hi is not None:
        size = _picked(answers.get("size_band"))
        if size == "within":
            fields["band"] = (lo, hi)
        elif size == "unknown":
            unknown.add("size")
        elif "size" in mandatory:
            return "fails_mandatory", {}
        else:
            return "outside_icp", {}

    if unknown & mandatory:
        return "fails_mandatory", {}
    return "", fields


# ---- the step ------------------------------------------------------------------------------------

async def _keep(survivors: list) -> dict[str, str]:
    """Every vetted company a search paid for goes into the shared store. ``{domain: id}``."""
    from nexus.companies.resolution import resolve_company
    from nexus.core.db import get_platform_sessionmaker

    ids: dict[str, str] = {}
    try:
        async with get_platform_sessionmaker()() as s:
            for row in survivors:
                async with s.begin_nested():
                    company = await resolve_company(s, domain=row.domain, name=row.name,
                                                    source="parallel")
                    if company is None:
                        continue
                    if row.description and not company.description:
                        company.description = row.description[:2000]
                    await s.flush()
                    ids[row.domain] = company.id
            await s.commit()
    except Exception:  # the store is an optimisation; failing to keep them loses no delivery
        logger.warning("could not keep Parallel companies in the shared store", exc_info=True)
    return ids


async def find_parallel_companies(ts, icp: dict, need: int, *, taken: set[str],
                                  client=None) -> ParallelStep:
    from nexus.prospecting import linkedin as li
    from nexus.prospecting.companies import Candidate, _held
    from nexus.prospecting.vetting import is_association, vet

    step = ParallelStep()
    objective = objective_for(icp)
    if need <= 0 or not objective:
        step.note = "" if need <= 0 else "no_icp"
        return step
    if client is None:
        from nexus.integrations.parallel import ParallelClient

        client = ParallelClient()

    found = await client.entity_search("companies", objective)
    if not found.ok:
        step.note = found.status if found.status in _STOPPING else "failed"
        return step
    step.searches += 0 if found.cached else 1

    rows, seen = [], set()
    for entity in found.entities:
        domain = li.company_domain(entity.url)
        if not domain:
            step.discarded["no_website"] += 1
        elif domain in seen or domain in taken:
            step.discarded["duplicate"] += 1
        elif is_association(entity.name):
            step.discarded["association"] += 1
        else:
            seen.add(domain)
            rows.append(SimpleNamespace(domain=domain, name=entity.name,
                                        description=entity.description))
    held = await _held(ts, [r.domain for r in rows])
    step.discarded["already_held"] += sum(1 for r in rows if r.domain in held)
    rows = [r for r in rows if r.domain not in held]
    rows, refused = await vet(rows)
    step.discarded["domain_not_theirs"] += len(refused)
    ids = await _keep(rows)

    questions = questions_for(icp)
    qhash = _question_hash(questions)
    gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def check(row):
        async with gate:
            return row, await client.decide(
                {"company": row.name, "website": f"https://{row.domain}",
                 "description": (row.description or "")[:500]},
                questions, cache_subject=f"{row.domain}|{qhash}")

    queue = list(rows)
    while queue and len(step.candidates) < need:
        batch, queue = queue[: need - len(step.candidates)], queue[need - len(step.candidates):]
        for row, outcome in await asyncio.gather(*(check(r) for r in batch)):
            if not outcome.ok:
                step.note = outcome.status if outcome.status in _STOPPING else "failed"
                queue = []
                continue
            step.decisions += 0 if outcome.cached else 1
            reason, fields = judge(outcome.answers, icp)
            if reason:
                step.discarded[reason] += 1
                continue
            if len(step.candidates) >= need:
                break
            ref = next((c["url"] for c in outcome.citations if c.get("url")), "")
            taken.add(row.domain)
            step.candidates.append(Candidate(
                domain=row.domain, name=row.name, source="parallel",
                company_id=ids.get(row.domain), industry=fields.get("industry"),
                country=fields.get("country"), url=ref,
                evidence={"ref": ref, "band": fields.get("band"),
                          "detail": "checked against the ICP with cited web evidence"}))
    if not step.candidates and not step.note:
        step.note = "nothing_new"
    if step.discarded:
        step.discarded = +step.discarded
    return step


async def record_costs(ts, *, searches: int, decisions: int) -> None:
    """What the step's calls cost us, on observe-only capabilities. Never raises."""
    from nexus.billing.meter import metered

    for capability, quantity in ((SEARCH_CAPABILITY, searches), (DECISION_CAPABILITY, decisions)):
        if quantity <= 0:
            continue
        try:
            async with metered(ts, capability, quantity=quantity, user_id=None,
                               attrs={"provider": "parallel"}):
                pass
        except Exception:  # observing a cost must never fail the search that incurred it
            logger.warning("could not record the cost of %s", capability, exc_info=True)
