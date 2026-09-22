"""Person and company engagement profiles, computed from identified facts (spec §18.5).

Pure: a list of fact dicts in, one profile dict out. The builder recomputes a profile from ALL of a
person's remaining facts whenever one of their facts changes, rather than incrementing counters, so
a workspace deletion or an erasure that removes facts leaves a profile that is exactly what the
remaining evidence says — never a count that still includes data somebody asked us to delete.
"""
from __future__ import annotations

from collections import Counter
from statistics import median

HOUR_S = 3600
DAY_S = 24 * HOUR_S

#: Response-speed bands of the last reply, the only pattern shown below the workspace threshold (D26).
BANDS = (("within_hour", HOUR_S), ("same_day", DAY_S), ("within_week", 7 * DAY_S))


def speed_band(latency_s: int | None) -> str:
    if latency_s is None or latency_s < 0:
        return ""
    for band, ceiling in BANDS:
        if latency_s <= ceiling:
            return band
    return "longer"


def _mode(values: list[int]) -> int | None:
    """Most frequent value; the smallest wins a tie, so the answer does not depend on fact order."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    counts = Counter(present)
    best = max(counts.values())
    return min(v for v, n in counts.items() if n == best)


def _aggregate(facts: list[dict]) -> dict:
    sends = [f for f in facts if f["fact_type"] == "send"]
    replies = sorted((f for f in facts if f["fact_type"] == "reply"), key=lambda f: f["occurred_at"])
    latencies = [f["response_latency_s"] for f in replies if f.get("response_latency_s") is not None]
    workspaces = sorted({f["workspace_key"] for f in facts if f.get("workspace_key")})
    return {
        "best_weekday": _mode([f.get("local_weekday") for f in replies]),
        "best_hour": _mode([f.get("local_hour") for f in replies]),
        "median_response_s": int(median(latencies)) if latencies else None,
        "reply_propensity": round(len(replies) / len(sends), 4) if sends else None,
        "sends": len(sends),
        "replies": len(replies),
        "workspace_count": len(workspaces),
        "workspace_keys": workspaces,
        "_last_reply": replies[-1] if replies else None,
    }


def person_profile(person_email: str, facts: list[dict]) -> dict | None:
    """``None`` when no facts remain: the profile is deleted rather than left describing nobody."""
    if not facts:
        return None
    agg = _aggregate(facts)
    last = agg.pop("_last_reply")
    ooo = sorted(
        ({"from": f["occurred_at"].date().isoformat() if hasattr(f["occurred_at"], "date")
          else str(f["occurred_at"])[:10],
          "until": (f.get("attrs") or {}).get("until", "")}
         for f in facts if f["fact_type"] == "out_of_office"),
        key=lambda p: p["from"],
    )[-5:]
    newest = max(facts, key=lambda f: f["occurred_at"])
    return {
        "person_email": person_email,
        "person_key": newest["person_key"],
        "company_domain": newest.get("company_domain") or "",
        **agg,
        "last_reply_band": speed_band(last.get("response_latency_s")) if last else "",
        "ooo_periods": ooo,
    }


def company_profile(company_domain: str, facts: list[dict]) -> dict | None:
    if not facts or not company_domain:
        return None
    agg = _aggregate(facts)
    agg.pop("_last_reply")
    return {"company_domain": company_domain, **agg}
