"""Which of the ICP's industries a new company really operates in (owner decision 2026-10-29).

SDR feedback: companies marked with the wrong industry, and companies that work in several. A
label from one source (LinkedIn's own category, a web find, another workspace's row) decided it,
and a supplier to an industry was filed under it. So every company delivered, whatever found it,
gets one check before it becomes an account, **LinkedIn first**:

1. What LinkedIn says about it: the industry and description the shared store already holds from a
   LinkedIn read, else one company-details run (the Apify actor the product already uses) for the
   pages it knows and has not read. linkedin.com itself is never fetched by our own crawlers:
   LinkedIn bans scrapers and serves bots a login wall.
2. One Parallel Decisions request (``verify.decision``, $0.001, cached 90 days by domain and
   question set) carrying LinkedIn's evidence, asking for EACH ICP industry whether the company
   operates in it, only sells to it, or neither, and which is its main business.

What it decides:

* operates in one or more ICP industries (each at 0.7 or more): ``industry`` is the main one among
  them, ``custom_fields.industries`` lists all of them, cited (`field_sources`, source
  ``parallel``); LinkedIn's own label, when different, is kept as ``industry_self_label``.
* only a supplier to them: delivered only when the ICP includes suppliers
  (``custom_fields.supplier_to``), else refused as ``wrong_industry``.
* a confident no on every one: refused as ``wrong_industry``.
* anything less certain: the source's label stands, as before. An unclear answer is no evidence
  either way.

Companies Parallel itself found are skipped: their industry was checked when they were found. With
no Parallel key nothing runs and delivery behaves as before.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.prospecting.industry_check")

MIN_CONFIDENCE = 0.7
MAX_CONCURRENT = 8
#: LinkedIn company pages read in one details run, at most.
MAX_LINKEDIN_READS = 25


@dataclass
class IndustryCheck:
    #: Domains not to deliver: their own business is in none of the ICP's industries.
    wrong: set[str] = field(default_factory=set)
    #: Decisions bought (not served from the cache), for the cost record.
    decisions: int = 0
    #: not_configured | out_of_credit | rejected | failed, or "" when it ran.
    note: str = ""


def _industries(icp: dict) -> list[str]:
    return [str(i).strip() for i in icp.get("industries") or [] if str(i).strip()]


def questions_for(icp: dict) -> dict:
    """Per ICP industry: in it, only a supplier to it, neither, or unknown; then the main one."""
    industries = _industries(icp)
    questions: dict = {}
    for i, label in enumerate(industries):
        questions[f"in_{i}"] = {
            "type": "choice",
            "instructions": f"Judged by what the company itself does (its own products, services "
                            f"and operations), does it operate in {label}?",
            "criteria": {
                "yes": f"Its own business is in {label}: it operates in {label} itself.",
                "supplier": f"It sells products or services to {label} companies but does not "
                            f"operate in {label} itself.",
                "no": f"It neither operates in {label} nor mainly sells to it.",
                "unknown": "The evidence does not establish this.",
            },
        }
    questions["primary"] = {
        "type": "choice",
        "instructions": "Which of these is the company's main business?",
        "criteria": {**{f"ind_{i}": label for i, label in enumerate(industries)},
                     "other": "Another industry.",
                     "unknown": "The evidence does not establish this."},
    }
    return questions


def _picked(answer) -> str:
    if answer is None or getattr(answer, "type", "") != "choice" or not answer.choice:
        return "unknown"
    prob = (answer.probabilities or {}).get(answer.choice)
    if prob is not None and float(prob) < MIN_CONFIDENCE:
        return "unknown"
    return answer.choice


@dataclass(frozen=True)
class Verdict:
    #: ICP industries it operates in, main one first.
    operates_in: tuple[str, ...] = ()
    supplier_to: tuple[str, ...] = ()
    #: Every industry answered a confident no.
    none: bool = False


def judge(answers: dict, icp: dict) -> Verdict:
    industries = _industries(icp)
    picks = [_picked(answers.get(f"in_{i}")) for i in range(len(industries))]
    operates = [label for label, pick in zip(industries, picks) if pick == "yes"]
    supplier = [label for label, pick in zip(industries, picks) if pick == "supplier"]
    primary = _picked(answers.get("primary"))
    if primary.startswith("ind_") and primary[4:].isdigit():
        main = industries[int(primary[4:])] if int(primary[4:]) < len(industries) else None
        if main in operates:
            operates.remove(main)
            operates.insert(0, main)
    return Verdict(operates_in=tuple(operates), supplier_to=tuple(supplier),
                   none=bool(picks) and all(p == "no" for p in picks))


async def _linkedin_evidence(built) -> dict[str, dict]:
    """``{domain: {industry, description, url}}`` from the shared store's LinkedIn reads, topped up
    by one details run for pages known but never read. Never raises."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company
    from nexus.prospecting.industries import all_industries

    labels = all_industries()
    out: dict[str, dict] = {}
    pages: dict[str, str] = {}
    domains = [c.domain for c, _ in built]
    try:
        async with get_platform_sessionmaker()() as s:
            rows = (await s.scalars(select(Company).where(Company.domain.in_(domains)))).all()
        for row in rows:
            if row.linkedin_fetched_at is not None:
                industry = labels.get(row.linkedin_industry_id)
                out[row.domain] = {"industry": industry.label if industry else (row.industry or ""),
                                   "description": (row.description or "")[:600],
                                   "url": row.linkedin_url or ""}
            elif row.linkedin_url:
                pages[row.domain] = row.linkedin_url
    except Exception:
        logger.warning("could not read LinkedIn evidence from the shared store", exc_info=True)
    for cand, _ in built:
        if cand.domain not in out and cand.domain not in pages and cand.linkedin_url:
            pages[cand.domain] = cand.linkedin_url
    if pages:
        out.update(await _read_pages(dict(list(pages.items())[:MAX_LINKEDIN_READS])))
    return out


async def _read_pages(pages: dict[str, str]) -> dict[str, dict]:
    """One company-details run for these pages. A page whose website is not this domain is not
    this company's (`linkedin.page_is_for`) and says nothing about it."""
    from nexus.prospecting import linkedin as li

    try:
        read = await li.company_details(list(pages.values()))
    except Exception as exc:  # unconfigured, refused or down: LinkedIn simply adds nothing
        logger.info("LinkedIn company read skipped: %r", exc)
        return {}
    out: dict[str, dict] = {}
    for domain, url in pages.items():
        company = read.get(li.canonical_company_url(url) or url)
        if company is not None and company.domain == domain:
            out[domain] = {"industry": company.industry_name, "url": company.linkedin_url,
                           "description": (company.description or "")[:600]}
    return out


def _question_hash(questions: dict) -> str:
    return hashlib.sha1(json.dumps(questions, sort_keys=True).encode("utf-8")).hexdigest()[:16]


async def check_industries(built, icp: dict, *, client=None) -> IndustryCheck:
    """Check every (candidate, account) pair not found by Parallel; writes the verdict onto the
    accounts (not yet in the session). Returns the domains to refuse and what it cost."""
    from nexus.integrations.provenance import record

    result = IndustryCheck()
    todo = [(c, a) for c, a in built if c.source != "parallel" and c.domain]
    if not todo or not _industries(icp):
        return result
    if client is None:
        from nexus.integrations.parallel import ParallelClient

        client = ParallelClient()
    configured = getattr(client, "configured", None)
    if configured is not None and not await configured():
        # No check to feed: the LinkedIn reads below would be spent for nothing.
        result.note = "not_configured"
        return result

    evidence = await _linkedin_evidence(todo)
    questions = questions_for(icp)
    qhash = _question_hash(questions)
    gate = asyncio.Semaphore(MAX_CONCURRENT)

    async def ask(cand, account):
        state = {"company": account.name or cand.name, "website": f"https://{cand.domain}"}
        seen = evidence.get(cand.domain) or {}
        if seen.get("industry"):
            state["linkedin_industry"] = seen["industry"]
        if seen.get("description"):
            state["linkedin_description"] = seen["description"]
        elif account.industry:
            state["listed_industry"] = account.industry
        async with gate:
            return await client.decide(state, questions,
                                       cache_subject=f"{cand.domain}|industries|{qhash}")

    outcomes = await asyncio.gather(*(ask(c, a) for c, a in todo))
    for (cand, account), outcome in zip(todo, outcomes):
        if not outcome.ok:
            if outcome.status in ("not_configured", "out_of_credit", "rejected"):
                result.note = outcome.status
            else:
                result.note = result.note or "failed"
            continue
        result.decisions += 0 if outcome.cached else 1
        verdict = judge(outcome.answers, icp)
        ref = next((c["url"] for c in outcome.citations if c.get("url")), None)
        custom = dict(account.custom_fields or {})
        seen_label = (evidence.get(cand.domain) or {}).get("industry") or account.industry
        if verdict.operates_in:
            if seen_label and seen_label != verdict.operates_in[0]:
                custom["industry_self_label"] = seen_label
            account.industry = verdict.operates_in[0]
            custom["industries"] = list(verdict.operates_in)
            account.custom_fields = custom
            record(account, "industry", "parallel", ref=ref,
                   detail="checked against each of your industries with cited web evidence")
        elif verdict.supplier_to:
            custom["supplier_to"] = list(verdict.supplier_to)
            account.custom_fields = custom
            if not icp.get("include_suppliers"):
                result.wrong.add(cand.domain)
        elif verdict.none:
            result.wrong.add(cand.domain)
    return result
