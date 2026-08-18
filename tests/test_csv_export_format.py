# tests/test_csv_export_format.py
"""An export has to open correctly in the thing people actually open it with.

Both endpoints produced *valid* CSV that was still wrong on a double-click:

* **No UTF-8 BOM** — Excel on Windows decodes a BOM-less file in the system codepage, so every
  non-ASCII character mangles. Real account names in this database contain emoji.
* **Raw ISO timestamps with microseconds** — `2026-08-05T10:35:59.957670+00:00` is text to a
  spreadsheet, not a date, so the column cannot be sorted or filtered.

These are not cosmetics. An export exists to be opened by someone who will not debug it.
"""
from __future__ import annotations

import csv
import io

from tests.conftest import auth, signup

BOM = "﻿"


async def _account(client, token, name: str, **kw):
    body = {"name": name, "domain": kw.pop("domain", None), **kw}
    r = await client.post("/api/accounts", headers=auth(token), json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def parse(text: str) -> tuple[list[str], list[list[str]]]:
    """Parse as a reader that honours the BOM would."""
    rows = list(csv.reader(io.StringIO(text.lstrip(BOM))))
    return rows[0], rows[1:]


# ---- the BOM -----------------------------------------------------------------------------------

async def test_accounts_export_starts_with_a_utf8_bom(client):
    token = await signup(client, slug="csv1", email="o@csv1.com", company="CSV1")
    await _account(client, token, "Acme", domain="acme.com")
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    assert r.status_code == 200
    assert r.text.startswith(BOM), "Excel needs the BOM to decode UTF-8"


async def test_contacts_export_starts_with_a_utf8_bom(client):
    token = await signup(client, slug="csv2", email="o@csv2.com", company="CSV2")
    r = await client.get("/api/contacts/export", headers=auth(token))
    assert r.text.startswith(BOM)


async def test_the_content_type_declares_utf8(client):
    """A browser previewing rather than downloading has to decode it too."""
    token = await signup(client, slug="csv3", email="o@csv3.com", company="CSV3")
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    assert "charset=utf-8" in r.headers["content-type"].lower()


async def test_non_ascii_survives_a_round_trip(client):
    """The reason the BOM matters: real account names carry emoji and accents."""
    token = await signup(client, slug="csv4", email="o@csv4.com", company="CSV4")
    await _account(client, token, "Café Solé 🚀", domain="cafe.example")
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    _, rows = parse(r.text)
    assert any("Café Solé 🚀" in row[0] for row in rows)


def test_the_bom_does_not_corrupt_the_first_header():
    """A reader that ignores the BOM must still see `name`, not `\\ufeffname`. This is the
    failure mode of adding a BOM carelessly."""
    from nexus.api.csv_export import csv_response

    body = csv_response("x.csv", ["name", "domain"], [["Acme", "acme.com"]]).body.decode()
    header, _ = parse(body)
    assert header[0] == "name"


# ---- timestamps --------------------------------------------------------------------------------

async def test_created_at_is_spreadsheet_readable(client):
    """`2026-08-05 10:35`, not `2026-08-05T10:35:59.957670+00:00`."""
    import re

    token = await signup(client, slug="csv5", email="o@csv5.com", company="CSV5")
    await _account(client, token, "Acme", domain="acme.com")
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    header, rows = parse(r.text)
    value = rows[0][header.index("created_at")]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value), value
    assert "T" not in value and "+" not in value and "." not in value


def test_a_missing_timestamp_is_blank_not_the_word_none():
    from nexus.api.csv_export import csv_timestamp

    assert csv_timestamp(None) == ""


# ---- structure ---------------------------------------------------------------------------------

async def test_a_value_containing_a_comma_is_quoted_not_split(client):
    """The whole point of using csv.writer rather than joining with commas."""
    token = await signup(client, slug="csv6", email="o@csv6.com", company="CSV6")
    await _account(client, token, "Acme, Inc.", domain="acme.example", industry="Tech, B2B")
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    header, rows = parse(r.text)
    row = next(x for x in rows if x[0] == "Acme, Inc.")
    assert row[header.index("industry")] == "Tech, B2B"


async def test_every_row_has_the_same_column_count_as_the_header(client):
    """A ragged CSV silently shifts every value into the wrong column."""
    token = await signup(client, slug="csv7", email="o@csv7.com", company="CSV7")
    await _account(client, token, "One", domain="one.example")
    await _account(client, token, "Two", domain="two.example", industry="SaaS", country="US")
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    header, rows = parse(r.text)
    assert rows and all(len(row) == len(header) for row in rows)


def test_none_cells_render_blank_rather_than_the_string_none():
    """`None` reaching a CSV as the literal text "None" is the classic export bug."""
    from nexus.api.csv_export import csv_response

    body = csv_response("x.csv", ["a", "b"], [["Acme", None]]).body.decode()
    _, rows = parse(body)
    assert rows[0] == ["Acme", ""]


async def test_the_download_is_named_and_attached(client):
    token = await signup(client, slug="csv8", email="o@csv8.com", company="CSV8")
    r = await client.get("/api/contacts/export", headers=auth(token))
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition and "contacts.csv" in disposition
