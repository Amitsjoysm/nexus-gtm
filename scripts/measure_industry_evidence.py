#!/usr/bin/env python
"""How accurate is the website industry reader, and how does LinkedIn's own label compare?

`companies/industry_evidence.py` stays off until this clears the precision bar on a hand-checked
sample (decided with the owner, 2026-10-26: "ensure they are highly accurate"). The sample is a CSV
of ``group,domain,name,accept``; ``accept`` lists LinkedIn codes whose whole subtree is a correct
answer (``57`` is Oil and Gas and its children). ``quality/industry-evidence/`` holds the one used.

For each company it reads the site and asks the model, exactly as delivery would, then reports:

* coverage: companies given a verdict at all;
* precision: verdicts inside the accepted subtrees, of those given;
* the same for LinkedIn's own label where the shared store holds one (the baseline).

Spends real money: up to two page fetches and two short AI calls per company. Prints the target
first. Writes nothing to any database.

    python scripts/measure_industry_evidence.py quality/industry-evidence/sample-2026-10-26.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The bar the owner's "highly accurate" is held to: of the companies given a verdict, this many
#: in the accepted subtree, with at least this share of the sample given one.
PRECISION_BAR = 0.95
COVERAGE_BAR = 0.80


def _accepted(nodes: str) -> set[int]:
    from nexus.prospecting.industries import descendants

    return descendants([int(n) for n in nodes.split("|") if n.strip().isdigit()])


async def _linkedin_codes(domains: list[str]) -> dict[str, int]:
    try:
        from sqlalchemy import select

        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.company import Company

        async with get_platform_sessionmaker()() as s:
            rows = (await s.execute(select(Company.domain, Company.linkedin_industry_id).where(
                Company.domain.in_(domains), Company.linkedin_fetched_at.isnot(None)))).all()
        return {d: c for d, c in rows if c}
    except Exception as exc:  # the baseline is optional
        print("no LinkedIn baseline:", repr(exc))
        return {}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--retries", type=int, default=4, help="re-asks while the model is busy")
    ap.add_argument("--out", help="write every row as JSON here")
    args = ap.parse_args()

    from nexus.companies import industry_evidence as ev
    from nexus.core.config import get_settings

    settings = get_settings()
    print("llm:", settings.llm_provider, getattr(settings, "llm_model", ""))
    dsn = getattr(settings, "db_owner_url", "") or settings.database_url
    print("database (LinkedIn baseline only):", re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn))

    rows = list(csv.DictReader(open(args.sample, encoding="utf-8")))

    async def ask(prompt: str) -> str:
        # A busy model (rate-limited, so the chain fell to the offline stub) answers "": that is
        # our outage, not a company without evidence, so it is asked again rather than scored.
        for attempt in range(args.retries + 1):
            text = await ev._default_ask(prompt)
            if text:
                return text
            await asyncio.sleep(15 * (attempt + 1))
        print("  model unavailable after retries")
        return ""

    verdicts = await ev.classify_many([(r["domain"], r["name"]) for r in rows],
                                      concurrency=args.concurrency, ask=ask)
    linkedin = await _linkedin_codes([r["domain"] for r in rows])

    out = []
    by_group: dict[str, Counter] = defaultdict(Counter)
    total = Counter()
    for r in rows:
        ok_codes = _accepted(r["accept"])
        v = verdicts.get(r["domain"])
        li = linkedin.get(r["domain"])
        result = "no_verdict" if v is None else ("correct" if v.code in ok_codes else "wrong")
        li_result = None if li is None else ("correct" if li in ok_codes else "wrong")
        for counter in (by_group[r["group"]], total):
            counter["n"] += 1
            counter[result] += 1
            if li_result:
                counter[f"li_{li_result}"] += 1
        out.append({**r, "result": result, "code": v.code if v else None,
                    "label": v.label if v else None, "quote": v.quote if v else None,
                    "url": v.url if v else None, "linkedin_code": li, "linkedin": li_result})
        mark = {"correct": "ok ", "wrong": "XX ", "no_verdict": "-- "}[result]
        print(f"{mark}{r['group']:24s} {r['domain']:30s} -> "
              f"{(v.label if v else '(no verdict)')[:44]:44s} | LinkedIn: "
              f"{ev.label_of(li)[:30] if li else '-':30s} {li_result or ''}")

    def summary(c: Counter) -> str:
        answered = c["correct"] + c["wrong"]
        cov = answered / c["n"] if c["n"] else 0.0
        prec = c["correct"] / answered if answered else 0.0
        li_n = c["li_correct"] + c["li_wrong"]
        li = f", LinkedIn label {c['li_correct']}/{li_n} correct" if li_n else ""
        return (f"{c['n']} companies, verdict for {answered} ({cov:.0%}), "
                f"{c['correct']} correct ({prec:.0%} precision){li}")

    print("\nby group:")
    for group, c in sorted(by_group.items()):
        print(f"  {group:24s} {summary(c)}")
    print("\nall:", summary(total))
    answered = total["correct"] + total["wrong"]
    precision = total["correct"] / answered if answered else 0.0
    coverage = answered / total["n"] if total["n"] else 0.0
    passed = precision >= PRECISION_BAR and coverage >= COVERAGE_BAR
    print(f"bar: precision >= {PRECISION_BAR:.0%} and coverage >= {COVERAGE_BAR:.0%}: "
          + ("PASSED" if passed else "NOT MET"))
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
