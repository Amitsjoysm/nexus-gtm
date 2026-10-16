"""The shared page cache (decision D10): bought once, sealed, expiring, and never a failure.

A page is the provider's answer to one query, the same for every workspace, so it is stored
platform-wide for 24 hours and the next workspace asking pays nothing. It holds the provider's PII,
so the column is Fernet-sealed and the row expires and is pruned. A failed call is never stored: a
cached failure would freeze an outage for the whole TTL.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.prospector_fixtures import CONTACTS, FakeVendor


def _client(vendor):
    from nexus.prospector.client import InfoJoyClient

    return InfoJoyClient(keys=["k"], transport=vendor.transport(), backoff_s=0)


async def _row(key):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.prospector import ProspectorPage

    async with get_platform_sessionmaker()() as s:
        return await s.get(ProspectorPage, key)


async def test_a_page_round_trips_and_the_column_holds_no_plaintext():
    from nexus.prospector import cache
    from nexus.prospector.records import ParsedPage

    key = cache.page_key("contacts", {"Titles": ["VP"]}, 1, 25)
    await cache.put_page(key, view="contacts", page=1, size=25,
                         parsed=ParsedPage(items=CONTACTS, total=4))

    got = await cache.get_page(key)
    assert got is not None and got.items == CONTACTS and got.total == 4
    row = await _row(key)
    assert "john.smith@acme-example.com" not in row.payload_sealed
    assert row.row_count == 4 and row.total_count == 4


async def test_the_key_is_stable_and_specific():
    from nexus.prospector.cache import page_key

    assert page_key("contacts", {"A": [1]}, 1, 25) == page_key("contacts", {"A": [1]}, 1, 25)
    assert page_key("contacts", {"A": [1]}, 1, 25) != page_key("companies", {"A": [1]}, 1, 25)
    assert page_key("contacts", {"A": [1]}, 1, 25) != page_key("contacts", {"A": [1]}, 2, 25)
    assert page_key("contacts", {"A": [1]}, 1, 25) != page_key("contacts", {"A": [1]}, 1, 50)


async def test_an_expired_page_is_a_miss_and_prune_removes_only_expired_rows():
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.prospector import ProspectorPage
    from nexus.prospector import cache
    from nexus.prospector.records import ParsedPage

    old = cache.page_key("contacts", {}, 1, 25)
    fresh = cache.page_key("contacts", {}, 2, 25)
    for key, page in ((old, 1), (fresh, 2)):
        await cache.put_page(key, view="contacts", page=page, size=25,
                             parsed=ParsedPage(items=CONTACTS[:1]))
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProspectorPage, old)
        row.expires_at = utcnow() - timedelta(minutes=1)
        await s.commit()

    assert await cache.get_page(old) is None
    assert await cache.prune() == 1
    assert await _row(old) is None and await _row(fresh) is not None


async def test_an_undecryptable_row_reads_as_a_miss():
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.prospector import ProspectorPage
    from nexus.prospector import cache
    from nexus.prospector.records import ParsedPage

    key = cache.page_key("contacts", {}, 3, 25)
    await cache.put_page(key, view="contacts", page=3, size=25, parsed=ParsedPage(items=[]))
    async with get_platform_sessionmaker()() as s:
        (await s.get(ProspectorPage, key)).payload_sealed = "not-a-fernet-token"
        await s.commit()

    assert await cache.get_page(key) is None


async def test_fetch_serves_the_second_identical_request_from_the_cache():
    from nexus.prospector.fetch import fetch_page

    vendor = FakeVendor()
    first = await fetch_page("contacts", {"Titles": ["VP"]}, page=1, size=2,
                             client=_client(vendor))
    second = await fetch_page("contacts", {"Titles": ["VP"]}, page=1, size=2,
                              client=_client(vendor))

    assert len(vendor.requests) == 1
    assert first.cached is False and first.vendor_rows == 2
    assert second.cached is True and second.vendor_rows == 0
    assert second.parsed.items == first.parsed.items


async def test_asking_for_a_count_refetches_a_page_cached_without_one():
    from nexus.prospector.fetch import fetch_page

    vendor = FakeVendor()
    await fetch_page("contacts", {}, page=1, size=2, client=_client(vendor))
    counted = await fetch_page("contacts", {}, page=1, size=2, count=True, client=_client(vendor))

    assert len(vendor.requests) == 2 and counted.parsed.total == len(CONTACTS)
    again = await fetch_page("contacts", {}, page=1, size=2, count=True, client=_client(vendor))
    assert len(vendor.requests) == 2 and again.cached is True


@pytest.mark.parametrize("status, http", [(503, 503), (401, 502), (429, 503)])
async def test_a_failure_is_raised_as_a_sentence_and_never_cached(status, http, monkeypatch):
    from nexus.prospector import cache
    from nexus.prospector.errors import ProspectorError
    from nexus.prospector.fetch import fetch_page

    async def record(*_a, **_k):
        return None

    monkeypatch.setattr("nexus.providers.service.record_rejection_from_response", record)
    vendor = FakeVendor(status=status)
    with pytest.raises(ProspectorError) as err:
        await fetch_page("contacts", {"Titles": ["x"]}, page=1, size=2, client=_client(vendor))
    assert err.value.status == http and err.value.detail
    assert await cache.get_page(cache.page_key("contacts", {"Titles": ["x"]}, 1, 2)) is None


async def test_not_configured_is_a_400_naming_provider_keys():
    from nexus.prospector.client import InfoJoyClient
    from nexus.prospector.errors import ProspectorError
    from nexus.prospector.fetch import fetch_page

    with pytest.raises(ProspectorError) as err:
        await fetch_page("contacts", {}, page=1, size=2,
                         client=InfoJoyClient(keys=[], transport=FakeVendor().transport()))
    assert err.value.status == 400 and "Provider keys" in err.value.detail


async def test_the_prune_job_is_registered_and_scheduled():
    import inspect

    from nexus.workers import scheduler, tasks

    assert "prune_prospector_pages" in tasks.HANDLERS
    assert "enqueue_prune_prospector_pages" in inspect.getsource(scheduler)
