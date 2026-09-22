"""Which backend answered has to be recorded, or the promotion decision is a guess.

`companies/diff.py` reads asymmetrically: results only the PAID provider found are the failure,
because those are what we would lose by promoting. The same read applies here, and it needs the
per-query record below plus a summary that counts it the right way round.
"""
from __future__ import annotations

from nexus.ingestion.sources import DorkedSearchSource
from nexus.integrations.search.fallback import FallbackSearchProvider
from nexus.integrations.search.provider import SearchHit
from nexus.models.account import Account


class Stub:
    def __init__(self, name, hits=None, failure=""):
        self.name = name
        self.query_dialect = "plain"
        self.last_failure = ""
        self._failure = failure
        self._hits = hits or []

    async def search_recent(self, query, *, limit=5, days=90, include_domains=(), exclude_domains=()):
        self.last_failure = self._failure
        return list(self._hits)


HIT = SearchHit(title="Acme raises $40M", url="https://x.test/a", snippet="", source="x")


def _account() -> Account:
    return Account(id="a1", tenant_id="t1", name="Acme", domain="acme.test")


async def test_the_answering_backend_is_recorded_per_query():
    chain = FallbackSearchProvider([Stub("nexusfetch", failure="blocked"), Stub("firecrawl", [HIT])])
    source = DorkedSearchSource(search=chain, max_queries=1)

    await source.fetch(_account())

    assert source.last_provenance["queries"][0]["answered_by"] == "firecrawl"


async def test_a_cached_query_is_recorded_as_answered_by_the_cache():
    chain = FallbackSearchProvider([Stub("nexusfetch", [HIT]), Stub("firecrawl", [HIT])])
    source = DorkedSearchSource(search=chain, max_queries=1)

    await source.fetch(_account())
    await source.fetch(_account())

    assert source.last_provenance["queries"][0]["answered_by"] == "cache"


async def test_a_shadow_run_records_what_each_backend_returned():
    chain = FallbackSearchProvider([Stub("nexusfetch", []), Stub("firecrawl", [HIT])], shadow=True)
    source = DorkedSearchSource(search=chain, max_queries=1)

    await source.fetch(_account())

    assert source.last_provenance["queries"][0]["shadow"] == {"nexusfetch": 0, "firecrawl": 1}


def test_the_summary_reads_the_comparison_the_right_way_round():
    from scripts.fetch_shadow_report import summarise

    runs = [
        {"queries": [{"cached": True, "answered_by": "cache"}]},
        {"queries": [{"answered_by": "nexusfetch"}]},
        {"queries": [{"answered_by": "firecrawl"}, {"failed": True}]},
        {"queries": [{"answered_by": "firecrawl", "shadow": {"nexusfetch": 0, "firecrawl": 3}}]},
        {"queries": [{"answered_by": "firecrawl", "shadow": {"nexusfetch": 2, "firecrawl": 2}}]},
        {"queries": [{"answered_by": "firecrawl", "shadow": {"nexusfetch": None, "firecrawl": 1}}]},
    ]

    summary = summarise(runs)

    assert summary["total"] == 7
    assert summary["cached"] == 1
    assert summary["failed"] == 1
    assert summary["answered"] == {"nexusfetch": 1, "firecrawl": 4}
    # The failure case: the paid engine found something the free one did not (or could not).
    assert summary["shadow"] == {"compared": 3, "paid_only": 2, "both": 1, "free_only": 0}
    assert summary["paid_queries"] == 4
