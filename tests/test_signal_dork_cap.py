# tests/test_signal_dork_cap.py
"""The dork query cap reaches a running process.

`DorkedSearchSource` used to copy `signal_dork_max_queries` at construction, inside the
process-wide ingestion service, so changing it from the Superadmin panel saved, read "in effect"
and changed nothing until a restart. Resetting that singleton instead is not an option: the test
suite injects its demo source through it.
"""
from __future__ import annotations

import inspect

from nexus.core.config import get_settings
from nexus.ingestion.sources import DorkedSearchSource
from nexus.models.account import Account


class _Counting:
    query_dialect = "operator"

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5):
        self.queries.append(query)
        return []

    async def search_recent(self, query: str, *, limit: int = 5, days: int = 90):
        return await self.search(query, limit=limit)


def _account() -> Account:
    return Account(name="Acme Corp", domain="acme.com", industry="Fintech")


async def test_a_changed_cap_reaches_a_source_that_already_exists(monkeypatch):
    monkeypatch.setattr(get_settings(), "signal_dork_max_queries", 3)
    search = _Counting()
    source = DorkedSearchSource(search=search)
    await source.fetch(_account())
    assert len(search.queries) == 3

    monkeypatch.setattr(get_settings(), "signal_dork_max_queries", 1)
    search.queries.clear()
    await source.fetch(_account())
    assert len(search.queries) == 1


async def test_an_explicit_cap_still_wins(monkeypatch):
    monkeypatch.setattr(get_settings(), "signal_dork_max_queries", 1)
    search = _Counting()
    await DorkedSearchSource(search=search, max_queries=3).fetch(_account())
    assert len(search.queries) == 3


def test_the_time_budget_follows_the_cap_in_force(monkeypatch):
    """A killed source reports nothing, so the budget must grow with the cap it actually runs."""
    source = DorkedSearchSource(pace_s=1.5)
    monkeypatch.setattr(get_settings(), "signal_dork_max_queries", 4)
    assert source.timeout_s == DorkedSearchSource(max_queries=4, pace_s=1.5).timeout_s
    monkeypatch.setattr(get_settings(), "signal_dork_max_queries", 1)
    assert source.timeout_s == DorkedSearchSource(max_queries=1, pace_s=1.5).timeout_s


def test_the_ingestion_service_does_not_freeze_the_cap():
    from nexus.ingestion import service

    assert "signal_dork_max_queries" not in inspect.getsource(service.get_ingestion_service)
