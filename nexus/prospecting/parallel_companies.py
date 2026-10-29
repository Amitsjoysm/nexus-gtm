"""New ICP companies from Parallel: Entity Search finds, one Decisions request per company checks.

Owner decisions 2026-10-29 (`docs/superpowers/specs/2026-10-29-parallel-provider-design.md`): the
chain is the shared store, LinkedIn, then this step, then Exa as the fallback; a company found here
is charged exactly as one Exa found (``discovery.account_added``), and what each call cost us is
recorded on ``search.entity`` / ``verify.decision``, never charged.

Every call counts:

* **One search asks for the 100 results its price includes** (Entity Search is priced per request),
  with an objective holding only the ICP's core criterion, industry and country, as its docs advise.
* **Free gates before any paid check**, read from the entity's record (`record_fields`): a company
  website of its own (``Website Url``; the entity's ``url`` is its LinkedIn or Tracxn page), not
  marked ``Deadpooled``, an HQ (``Location Country``) in the ICP's countries, not held, not taken,
  not an association, a domain that is the company's (`vetting.vet`).
* **Its homepage, read just before its check** (`vetting.lands_elsewhere`): a site that now lands
  on another company's domain is refused without paying for the check.
* **One Decisions request per company carries every check** (still operating under its own name
  with its own website, industry by what the company itself does, HQ country, size band), asked a
  batch at a time and only until the request is met. A verdict is cached 90 days by domain and
  question set.
* **A company is kept in the shared store once its check says it operates under its own name**,
  whatever it says about this ICP, so the next workspace takes it from the store without anyone
  paying again. An unchecked or refused one is not kept: other workspaces would be offered it.

The record is a lead, not proof. Measured on the first real search (100 US oil and gas companies,
2026-10-29): 25 marked ``Deadpooled``, 13 ``Acquired`` (Chevron and Murphy Oil among them, which
were not: their records describe deals they made), and bankrupt or merged companies still marked
``Public`` (Cobalt International, Cabot Oil). So ``Deadpooled`` is the only stage refused for free;
whether a company still operates is the check's question. The record's headcount answers a size
only where the check could not (`_size_from_record`): it was close for most, but read 6 for Delek
US Holdings, so it never overrides the check, and the check is not told it.

A company is delivered when the check says, at 70% or more (95% when the record says Acquired),
that it operates under its own name with its own website and is in the ICP's industry, and nothing
it answered falls outside the ICP. A size or country it could not answer goes to delivery as
``unchecked``, which looks it up and screens it as it does a web find; a must-match field still
without data is no match (D3). Other choices under 50% are read as unknown.

Measured on the first real run, 2026-10-29: of 14 companies whose fate is known, the check alone
called four dead or bought ones operating (Cabot, Cobalt, Midstates, Triangle); the homepage and
the confidence rules catch three of the four, and Bison and Clayton Williams besides. Cobalt
(bankrupt 2018, its site still up) is still missed.
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
#: Identity and industry are what make it this account at all, so even odds are not enough.
#: Measured: Midstates (merged 2019) and Triangle (bankrupt) read "own site" at 0.56 and 0.58;
#: SPM Oil & Gas, a supplier, read as an operator at 0.51. Devon, live, read 0.73.
IDENTITY_CONFIDENCE = 0.7
INDUSTRY_CONFIDENCE = 0.7
#: When the record itself says ``Acquired``, the check must be all but certain it still operates:
#: Bison (bought 2022) and Clayton Williams (2017) read 0.90 and 0.79; Chevron and Murphy Oil,
#: whose records say Acquired for deals they made, read 0.998 and 0.993.
ACQUIRED_STAGE_CONFIDENCE = 0.95
#: Why a company whose identity check failed was refused, by the option chosen.
IDENTITY_REFUSAL = {"acquired": "acquired", "defunct": "defunct", "parent_site": "not_its_own",
                    "unknown": "unconfirmed"}
_IDENTITY_FAILURES = frozenset({*IDENTITY_REFUSAL.values(), "lands_elsewhere"})
#: The one ``Stage`` refused without a check: the record itself says the company is gone.
DEFUNCT_STAGES = frozenset({"deadpooled"})
ACQUIRED_STAGES = frozenset({"acquired"})
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
            "instructions": "Does this company still operate today, under its own name, and is "
                            "the website given in the state its own site?",
            "criteria": {
                "own_site": "It operates today under its own name, and the website is its own "
                            "site.",
                "parent_site": "The website belongs to a parent, owner or acquirer, not to this "
                               "company itself.",
                "acquired": "It was acquired by or merged into another company and no longer "
                            "operates under its own name.",
                "defunct": "It has stopped operating: closed, bankrupt, dissolved or no longer "
                           "trading.",
                "unknown": "The evidence does not establish whether it operates or whose website "
                           "it is.",
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

def _picked(answer, at_least: float = MIN_CONFIDENCE) -> str:
    """The chosen option, or ``unknown`` for a refusal, a missing answer or a choice under
    ``at_least``."""
    if answer is None or getattr(answer, "type", "") != "choice" or not answer.choice:
        return "unknown"
    prob = (answer.probabilities or {}).get(answer.choice)
    if prob is not None and float(prob) < at_least:
        return "unknown"
    return answer.choice


def judge(answers: dict, icp: dict, *, stage: str = "") -> tuple[str, dict]:
    """``("", fields)`` to deliver, else ``(reason, {})``. ``fields`` holds the industry and
    country labels and the employee band the answers support, and ``unchecked``: the size or
    country the check could not answer. Those are not refused here: delivery looks them up and
    screens them, and a must-match field with no data after that is no match (D3). Measured, the
    check could not size 38 of 79 companies, Diamondback, EOG and Devon among them."""
    from nexus.prospecting.companies import country_label
    from nexus.relevance.gate import mandatory_fields

    mandatory = set(mandatory_fields(icp))
    need = ACQUIRED_STAGE_CONFIDENCE if stage in ACQUIRED_STAGES else IDENTITY_CONFIDENCE
    identity = _picked(answers.get("identity"), need)
    if identity != "own_site":
        return IDENTITY_REFUSAL.get(identity, "unconfirmed"), {}
    industries = _clean_list(icp.get("industries"))
    countries = _clean_list(icp.get("countries"))
    fields: dict = {}
    unchecked: list[str] = []

    industry = _picked(answers.get("industry"), INDUSTRY_CONFIDENCE)
    if industry.startswith("ind_") and industry[4:].isdigit() and int(industry[4:]) < len(industries):
        fields["industry"] = industries[int(industry[4:])]
    elif industry == "supplier" and icp.get("include_suppliers"):
        fields["industry"] = industries[0]
    elif industry == "unknown":
        # The search was FOR this industry: a company the check cannot place in it is not one.
        return "industry_unclear", {}
    else:
        return "wrong_industry", {}

    if "hq_country" in answers or countries:
        country = _picked(answers.get("hq_country"))
        if country.startswith("c_") and country[2:].isdigit() and int(country[2:]) < len(countries):
            from nexus.contacts.phone import region_for

            label = countries[int(country[2:])]
            fields["country"] = country_label(region_for(label)) or label
        elif country == "unknown":
            unchecked.append("country")
        else:
            return "outside_icp", {}

    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if lo is not None or hi is not None:
        size = _picked(answers.get("size_band"))
        if size == "within":
            fields["band"] = (lo, hi)
        elif size == "unknown":
            unchecked.append("size")
        elif "size" in mandatory:
            return "fails_mandatory", {}
        else:
            return "outside_icp", {}

    fields["unchecked"] = unchecked
    return "", fields


# ---- the step ------------------------------------------------------------------------------------

def _row_for(entity) -> SimpleNamespace:
    """What one entity says about itself: its website's domain, what it does, where it is."""
    from nexus.integrations.parallel import record_fields
    from nexus.prospecting import linkedin as li

    fields = record_fields(entity.description)
    domain = li.company_domain(fields.get("Website Url") or "") or li.company_domain(entity.url)
    if fields:
        about = " ".join(v for v in (fields.get("Short Description"),
                                     fields.get("Detailed Description")) if v)
    else:
        about = entity.description or ""
    hq = ", ".join(v for v in (fields.get("Location City"), fields.get("Location State"),
                               fields.get("Location Country")) if v)
    return SimpleNamespace(domain=domain, name=entity.name, about=" ".join(about.split()), hq=hq,
                           country=fields.get("Location Country") or "",
                           stage=(fields.get("Stage") or "").strip().lower(),
                           headcount=_headcount(fields.get("Employee Count Official")))


def _headcount(value) -> int | None:
    try:
        count = int(float(str(value or "").replace(",", "")))
    except ValueError:
        return None
    return count if count > 0 else None


def _size_from_record(row, icp: dict) -> str:
    """For a size the check could not answer: ``within``/``outside`` by the record's own headcount,
    ``""`` when it has none. Measured against known headcounts (2026-10-29) it was close for
    Diamondback, Gulfport, Ring, Magnolia and Cheniere, and wrong for Delek (6) and Devon (5,637,
    about 2,300), so it fills in only where the check is silent, never over it."""
    if row.headcount is None:
        return ""
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if (lo is not None and row.headcount < lo) or (hi is not None and row.headcount > hi):
        return "outside"
    return "within"


def _outside_countries(row, icp: dict) -> bool:
    """The record names an HQ country, and it is not one of the ICP's."""
    from nexus.contacts.phone import region_for

    wanted = {region_for(c) for c in _clean_list(icp.get("countries"))} - {""}
    have = region_for(row.country)
    return bool(wanted and have and have not in wanted)


def _state_for(row) -> dict:
    """What a check is told: name, website, HQ and what it does. Not the record's stage or
    employee count, which are often stale (see the module docstring) and would steer the answer."""
    state = {"company": row.name, "website": f"https://{row.domain}"}
    if row.hq:
        state["headquarters"] = row.hq
    if row.about:
        state["description"] = row.about[:500]
    return state


async def _keep(survivors: list) -> dict[str, str]:
    """Companies whose check said they operate under their own name go into the shared store.
    ``{domain: id}``."""
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
                    if row.about and not company.description:
                        company.description = row.about[:2000]
                    await s.flush()
                    ids[row.domain] = company.id
            await s.commit()
    except Exception:  # the store is an optimisation; failing to keep them loses no delivery
        logger.warning("could not keep Parallel companies in the shared store", exc_info=True)
    return ids


async def find_parallel_companies(ts, icp: dict, need: int, *, taken: set[str],
                                  client=None) -> ParallelStep:
    from nexus.prospecting.companies import Candidate, _held
    from nexus.prospecting.vetting import is_association, lands_elsewhere, vet

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
        row = _row_for(entity)
        if not row.domain:
            step.discarded["no_website"] += 1
        elif row.stage in DEFUNCT_STAGES:
            step.discarded["defunct"] += 1
        elif row.domain in seen or row.domain in taken:
            step.discarded["duplicate"] += 1
        elif is_association(row.name):
            step.discarded["association"] += 1
        elif _outside_countries(row, icp):
            step.discarded["outside_icp"] += 1
        else:
            seen.add(row.domain)
            rows.append(row)
    held = await _held(ts, [r.domain for r in rows])
    step.discarded["already_held"] += sum(1 for r in rows if r.domain in held)
    rows = [r for r in rows if r.domain not in held]
    rows, refused = await vet(rows)
    step.discarded["domain_not_theirs"] += len(refused)

    questions = questions_for(icp)
    qhash = _question_hash(questions)
    gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def check(row):
        async with gate:
            # Free before paid: a homepage that lands on another company settles it.
            if await lands_elsewhere(row.domain, row.name):
                return row, None
            return row, await client.decide(_state_for(row), questions,
                                            cache_subject=f"{row.domain}|{qhash}")

    own, picked = [], []
    queue = list(rows)
    while queue and len(picked) < need:
        batch, queue = queue[: need - len(picked)], queue[need - len(picked):]
        for row, outcome in await asyncio.gather(*(check(r) for r in batch)):
            if outcome is None:
                step.discarded["lands_elsewhere"] += 1
                continue
            if not outcome.ok:
                step.note = outcome.status if outcome.status in _STOPPING else "failed"
                queue = []
                continue
            step.decisions += 0 if outcome.cached else 1
            reason, fields = judge(outcome.answers, icp, stage=row.stage)
            if reason not in _IDENTITY_FAILURES:
                own.append(row)
            headcount = None
            if not reason and "size" in fields["unchecked"]:
                by_record = _size_from_record(row, icp)
                if by_record == "outside":
                    reason = "outside_icp"
                elif by_record == "within":
                    fields["unchecked"] = [f for f in fields["unchecked"] if f != "size"]
                    headcount = row.headcount
            if reason:
                step.discarded[reason] += 1
            elif len(picked) < need:
                taken.add(row.domain)
                picked.append((row, fields, headcount,
                               next((c["url"] for c in outcome.citations if c.get("url")), "")))
    ids = await _keep(own)
    for row, fields, headcount, ref in picked:
        step.candidates.append(Candidate(
            domain=row.domain, name=row.name, source="parallel",
            company_id=ids.get(row.domain), industry=fields.get("industry"),
            country=fields.get("country"), employee_count=headcount, url=ref,
            evidence={"ref": ref, "band": fields.get("band"),
                      "unchecked": fields.get("unchecked") or [],
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
