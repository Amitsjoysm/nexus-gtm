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
