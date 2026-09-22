"""Parsing is tested against a saved page, never the network.

The engines change their markup; when they do, this test fails with a real diff instead of the
product quietly reporting "no signals". `blocked` is a distinct outcome from "no results" for the
same reason `signal_source_runs` separates `error` from `empty`.

`fixtures/serp_ddg.html` is a real DuckDuckGo HTML results page for "vanta series c funding",
saved 2026-09-22.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.fetching.parse import BlockedByEngine, parse_ddg

FIXTURE = Path(__file__).parent / "fixtures" / "serp_ddg.html"


def test_results_are_extracted_with_titles_and_absolute_urls():
    hits = parse_ddg(FIXTURE.read_text(encoding="utf-8"), limit=5)

    assert len(hits) == 5
    for hit in hits:
        assert hit["title"]
        assert hit["url"].startswith("https://")
        assert "duckduckgo.com" not in hit["url"], "a redirect wrapper was not unwrapped"
        assert hit["source"] == "nexusfetch:ddg"


def test_the_redirect_is_unwrapped_to_the_real_page():
    first = parse_ddg(FIXTURE.read_text(encoding="utf-8"), limit=1)[0]
    assert first["url"] == "https://www.vanta.com/resources/vanta-announces-series-c"


def test_entities_in_titles_and_snippets_are_decoded():
    hits = parse_ddg(FIXTURE.read_text(encoding="utf-8"), limit=10)
    assert not any("&#x27;" in h["snippet"] or "&amp;" in h["title"] for h in hits)


def test_an_anti_bot_page_raises_blocked_rather_than_returning_nothing():
    blocked_page = "<html><body>Unfortunately, bots use DuckDuckGo too. Please try again.</body></html>"
    with pytest.raises(BlockedByEngine):
        parse_ddg(blocked_page, limit=5)


def test_a_page_with_no_results_is_an_empty_answer_not_a_block():
    assert parse_ddg("<html><body><div class='no-results'>No results.</div></body></html>") == []


def test_a_sponsored_result_is_dropped():
    # Ads link through DuckDuckGo's own tracker, not a `uddg=` redirect, so after unwrapping they
    # still point at duckduckgo.com. An ad for a competitor is not an event about the account.
    page = (
        '<a class="result__a" href="//duckduckgo.com/y.js?ad_domain=rival.test&amp;u3=x">Rival ad</a>'
        '<a class="result__snippet" href="//duckduckgo.com/y.js?x">Buy now</a>'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.test%2Fnews&amp;rut=1">'
        'Acme raises $40M</a>'
        '<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.test%2Fnews">'
        'Series B</a>'
    )
    hits = parse_ddg(page, limit=5)
    assert [h["url"] for h in hits] == ["https://acme.test/news"]
    assert hits[0]["snippet"] == "Series B"
