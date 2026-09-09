# tests/test_feed_text.py
"""Feed text has to be readable by the time a rep sees it.

Reported from the live app: RSS-sourced signals showed raw entity codes on the Signals list, the
account Signals tab and the dashboard activity feed — `&#8211;` where a dash belonged, `&#8217;s`
for an apostrophe, and WordPress's `[&#8230;]` read-more marker.

Measured on the live database before the fix: **73 of 139 stored RSS signals carried entity codes
and 74 carried HTML tags**. Not an edge case — the majority of the feed.

The cause is double encoding, which is the norm rather than the exception for RSS: the publisher
HTML-escapes the content and the XML layer escapes it again, so `&amp;#8211;` survives XML parsing
as the literal text `&#8211;`. One `html.unescape` is not enough, which is why the fix is ordered
rather than a single call.
"""
from __future__ import annotations

import pytest

from nexus.ingestion.sources import _parse_feed, clean_feed_text


# ---- the exact strings that were reported ------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("&#8211;", "–"),
        ("Vanta&#8217;s new round", "Vanta’s new round"),
        ("Read more [&#8230;]", "Read more […]"),
        ("A &amp; B", "A & B"),
        ("&amp;#8211; double encoded", "– double encoded"),
    ],
)
def test_the_reported_entities_decode(raw, expected):
    assert clean_feed_text(raw) == expected


def test_markup_is_removed_not_shown():
    """74 of 139 rows carried tags. A `<p>` rendered as literal text is the same class of bug."""
    assert clean_feed_text("<p>Hello &#8211; world</p>") == "Hello – world"
    # Escaped tags have to survive the first unescape and then be stripped — this is why the
    # order is unescape, strip, unescape rather than a single pass.
    assert clean_feed_text("&lt;p&gt;Escaped tags&lt;/p&gt;") == "Escaped tags"


def test_ordinary_text_is_untouched():
    assert clean_feed_text("Plain title") == "Plain title"
    # Case is preserved: this is display text, unlike `webwatch.normalise` which lowercases
    # because it feeds a hash.
    assert clean_feed_text("Acme Raises Series B") == "Acme Raises Series B"


def test_blank_input_is_blank_not_an_error():
    assert clean_feed_text("") == ""
    assert clean_feed_text(None) == ""


def test_whitespace_from_stripped_markup_collapses():
    assert clean_feed_text("<div>  a  </div>\n<div> b </div>") == "a b"


# ---- through the parser, where it actually runs -------------------------------------------------

def test_a_double_encoded_feed_parses_clean():
    xml = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>Acme &amp;#8211; Series B</title>
        <link>https://acme.example/news/1</link>
        <description>&lt;p&gt;Acme raised &amp;#8230; today&lt;/p&gt;</description>
      </item>
    </channel></rss>"""
    items = _parse_feed(xml)
    assert len(items) == 1
    assert items[0]["title"] == "Acme – Series B"
    assert items[0]["summary"] == "Acme raised … today"


def test_the_link_is_left_exactly_as_published():
    """A URL is not display text. Unescaping a query string would corrupt it — `&amp;` between
    parameters is meaningful, and turning it into a bare `&` is fine, but stripping anything that
    looks like a tag is not."""
    xml = """<?xml version="1.0"?>
    <rss><channel><item>
      <title>T</title>
      <link>https://x.example/a?b=1&amp;c=2</link>
    </item></channel></rss>"""
    items = _parse_feed(xml)
    # XML decodes &amp; to & — that is correct and is the URL as published.
    assert items[0]["link"] == "https://x.example/a?b=1&c=2"


# ---- markdown, not just HTML ---------------------------------------------------------------------

def test_markdown_navigation_links_are_not_the_story():
    """Measured on the live deployment: 29 stored search-backed signals opened with
    "[Skip to content](...)" followed by a run of "[Share on Facebook](...)". A search provider
    asked for page CONTENT returns markdown, and its links are the page's chrome — served into the
    one line a rep reads to decide whether an account is worth touching."""
    from nexus.ingestion.sources import clean_feed_text

    got = clean_feed_text(
        "[Skip to content](https://techcrunch.com/x/#wp--skip-link--target) "
        "Stripe valuation soars 74% to $159 billion "
        "[Share on Facebook](https://www.facebook.com/sharer.php?u=x)"
    )
    assert "](" not in got and "http" not in got
    assert "Stripe valuation soars 74%" in got


def test_the_link_label_survives_because_it_is_often_the_sentence():
    from nexus.ingestion.sources import clean_feed_text

    assert clean_feed_text("[Acme raises $40M](https://x.com/a)") == "Acme raises $40M"


def test_images_and_bare_urls_go():
    from nexus.ingestion.sources import clean_feed_text

    assert clean_feed_text("![logo](https://x.com/a.png) Acme raises $40M.") == "Acme raises $40M."
    assert clean_feed_text("Read more at https://example.com/a?b=c now.") == "Read more at now."


def test_headings_and_emphasis_are_unwrapped():
    from nexus.ingestion.sources import clean_feed_text

    got = clean_feed_text("# Stockoscope (Pty Ltd.)  **Stockoscope** is a _Financial Services_ company.")
    assert got == "Stockoscope (Pty Ltd.) Stockoscope is a Financial Services company."


def test_ordinary_prose_is_untouched():
    """The cleaner runs on every feed and search body. Mangling text that was already fine would
    trade one display bug for a subtler one."""
    from nexus.ingestion.sources import clean_feed_text

    plain = "Acme raised $40M led by Sequoia to expand European operations."
    assert clean_feed_text(plain) == plain


def test_both_search_backed_sources_clean_their_snippet():
    """RSS was cleaned from the start; the two SEARCH paths set `body=snippet` raw. Structural,
    because the fault only shows when a provider returns page content rather than a sentence."""
    import inspect

    from nexus.ingestion import sources

    src = inspect.getsource(sources)
    assert src.count("body=clean_feed_text(snippet) or None") == 2, (
        "a search-backed source is storing an uncleaned snippet again"
    )
