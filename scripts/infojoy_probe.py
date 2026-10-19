#!/usr/bin/env python
"""Observe the InfoJoy Search API before any parser trusts it (Prospector, decision D25).

CLAUDE.md requires one real call to every external provider before its parser is written: field
names, casing and value vocabularies are not a contract until seen. This makes a handful of SMALL
calls (each page ≤ 50 rows; roughly 300 records in all, at most about $1.50 at the top of the quoted
range) and prints what came back with every person-identifying value redacted:

* the top-level and item keys of both endpoints, with their JSON types;
* the name of the total-count field when ``IncludeTotalCount`` is true;
* the distinct values of the enum-like fields (seniority, department, size, industry, places);
* which location filter names the contacts endpoint actually honours. A recognised filter given a
  value that cannot exist returns no rows; an ignored one returns rows as if it were not there. That
  difference is how the person-level and company-level location sets (D15) are told apart without
  guessing what the provider called them;
* ``HasDirectPhone`` true vs false, and the documented error for a page over 500.

It writes nothing anywhere. **Run it where the key is**, inside the app container, so it reads the
key a superadmin stored in Provider keys (or the environment fallback):

    docker cp scripts/infojoy_probe.py nexus-gtm-app-1:/tmp/infojoy_probe.py
    docker exec nexus-gtm-app-1 python /tmp/infojoy_probe.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PII = re.compile(r"(email|phone|name|url|linkedin|zip)", re.I)
_ENUMISH = ("department", "senioritylevel", "country", "state", "city", "industrytype",
            "employeesizerange", "revenuerange", "subindustry")
_IMPOSSIBLE = "Zzqx Nowhere 0000"
_LOCATION_CANDIDATES = (
    "Countries", "States", "Cities", "ZipCodes",
    "ContactCountries", "ContactStates", "ContactCities", "ContactZipCodes",
    "PersonCountries", "PersonStates", "PersonCities", "PersonZipCodes",
    "CompanyCountries", "CompanyStates", "CompanyCities", "CompanyZipCodes",
    "HqCountries", "HqStates", "HqCities", "HqZipCodes",
)


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _redact(item: dict) -> dict:
    out = {}
    for key, value in item.items():
        if _PII.search(key) and value not in (None, ""):
            out[key] = f"<redacted:{type(value).__name__}:{len(str(value))}>"
        else:
            out[key] = value
    return out


def _types(obj: dict) -> dict:
    return {k: type(v).__name__ for k, v in obj.items()}


#: Seconds each call took, by label: the client's timeouts are set from these, not guessed.
LATENCY: dict[str, float] = {}


async def _post(client, base: str, path: str, key: str, filters: dict, *, size: int = 5,
                count: bool = False, label: str = ""):
    """``(status, body)``. A timeout or transport error is reported, never raised, so one slow
    query does not hide what the others would have shown."""
    import time

    import httpx

    body = {"PageNumber": 1, "PageSize": size, "IncludeTotalCount": count, "Filters": filters}
    started = time.monotonic()
    try:
        resp = await client.post(f"{base}{path}", headers={"X-Api-Token": key}, json=body)
    except httpx.TimeoutException:
        LATENCY[label or path] = round(time.monotonic() - started, 1)
        return "timeout", {}
    except httpx.HTTPError as exc:
        LATENCY[label or path] = round(time.monotonic() - started, 1)
        return f"transport:{type(exc).__name__}", {}
    LATENCY[label or path] = round(time.monotonic() - started, 1)
    try:
        data = resp.json()
    except ValueError:
        data = {"_unparseable": resp.text[:200]}
    return resp.status_code, data


def _items(data) -> list[dict]:
    if not isinstance(data, dict):
        return []
    for k, v in data.items():
        if _norm(k) == "items" and isinstance(v, list):
            return [i for i in v if isinstance(i, dict)]
    return []


def _distinct(items: list[dict]) -> dict[str, list]:
    seen: dict[str, Counter] = {}
    for item in items:
        for k, v in item.items():
            if _norm(k) in _ENUMISH and v not in (None, ""):
                seen.setdefault(k, Counter())[str(v)] += 1
    return {k: [v for v, _ in c.most_common(40)] for k, c in seen.items()}


async def main() -> int:
    import httpx

    from nexus.core.config import get_settings
    from nexus.providers.resolver import key_pool

    keys = await key_pool("infojoy_search")
    if not keys:
        print("No InfoJoy Search key: store one in Superadmin > Provider keys first.")
        return 2
    key = keys[0]
    base = get_settings().infojoy_search_base_url.rstrip("/")
    contacts, companies = "/api/search/contacts", "/api/search/companies"
    report: dict = {"base_url_host": httpx.URL(base).host}

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        # Smallest, narrowest first: the documented sample request, so a slow broad query cannot
        # stop us learning the shape.
        status, data = await _post(client, base, contacts, key,
                                   {"Titles": ["manager"], "SeniorityLevels": ["Manager"],
                                    "Countries": ["United States"]}, size=10, label="sample")
        report["documented_sample"] = {"status": status,
                                       "item_keys": _types(_items(data)[0]) if _items(data) else {},
                                       "sample_redacted": [_redact(i) for i in _items(data)[:2]]}
        status, data = await _post(client, base, contacts, key,
                                   {"Countries": ["United States"]}, size=50,
                                   label="contacts_50")
        items = _items(data)
        report["contacts"] = {
            "status": status,
            "top_level": _types(data) if isinstance(data, dict) else type(data).__name__,
            "item_keys": _types(items[0]) if items else {},
            "sample_redacted": [_redact(i) for i in items[:2]],
            "distinct": _distinct(items),
        }
        status, data = await _post(client, base, contacts, key,
                                   {"Countries": ["United States"]}, size=1, count=True,
                                   label="contacts_count")
        report["contacts_with_count"] = {"status": status, "top_level": _types(data)
                                         if isinstance(data, dict) else type(data).__name__}

        status, data = await _post(client, base, companies, key,
                                   {"Countries": ["United States"]}, size=50,
                                   label="companies_50")
        items = _items(data)
        report["companies"] = {
            "status": status,
            "top_level": _types(data) if isinstance(data, dict) else type(data).__name__,
            "item_keys": _types(items[0]) if items else {},
            "sample_redacted": [_redact(i) for i in items[:2]],
            "distinct": _distinct(items),
        }
        status, data = await _post(client, base, companies, key,
                                   {"Countries": ["United States"]}, size=1, count=True,
                                   label="companies_count")
        report["companies_with_count"] = {"status": status, "top_level": _types(data)
                                          if isinstance(data, dict) else type(data).__name__}

        honoured = {}
        for name in _LOCATION_CANDIDATES:
            status, data = await _post(client, base, contacts, key, {name: [_IMPOSSIBLE]}, size=3,
                                       label=f"location:{name}")
            rows = len(_items(data))
            honoured[name] = {"status": status, "rows_for_impossible_value": rows,
                              "honoured": status == 200 and rows == 0}
        report["contacts_location_filters"] = honoured

        phone = {}
        for flag in (True, False):
            status, data = await _post(client, base, contacts, key,
                                       {"Countries": ["United States"], "HasDirectPhone": flag},
                                       size=20, label=f"phone:{flag}")
            items = _items(data)
            with_phone = sum(1 for i in items
                             for k, v in i.items() if _norm(k) == "directphone" and v)
            phone[str(flag)] = {"status": status, "rows": len(items), "with_phone": with_phone}
        report["has_direct_phone"] = phone

        status, data = await _post(client, base, contacts, key, {"Titles": ["manager"]}, size=501,
                                   label="page_size_501")
        report["page_size_501"] = {"status": status, "body": data}
        # What the panel's probe sent, which answered 500.
        status, data = await _post(client, base, contacts, key, {"Titles": ["manager"]}, size=1,
                                   label="panel_probe")
        report["panel_probe"] = {"status": status, "rows": len(_items(data))}

    report["latency_s"] = LATENCY
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
