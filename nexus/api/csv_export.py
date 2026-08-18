# nexus/api/csv_export.py
"""One place that turns rows into a CSV a spreadsheet opens correctly.

Both export endpoints built their own `Response`, and both produced a file that is *valid* CSV and
still wrong when a human double-clicks it:

* **No UTF-8 BOM.** Excel on Windows opens a BOM-less CSV in the system codepage, so every
  non-ASCII character is mangled. Measured on real account names: emoji and accented characters
  came through as mojibake. A BOM is three bytes that make the difference between a file that
  opens and a file that opens wrong — and every other reader (Sheets, Numbers, pandas, `csv`)
  skips it.
* **Raw ISO timestamps with microseconds.** `2026-08-05T10:35:59.957670+00:00` is not a date to a
  spreadsheet; it lands as text, so it cannot be sorted or filtered. `2026-08-05 10:35` is
  recognised, sorts correctly as a string *and* as a date, and is what a person wanted anyway.

Neither is a formatting nicety. An export exists to be opened by someone who is not going to
debug it.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Iterable, Sequence

from fastapi import Response

# Excel's marker for "this file is UTF-8". Other readers treat it as a zero-width no-break space
# and skip it, which is why it is safe to add unconditionally.
_BOM = "﻿"


def csv_timestamp(value: datetime | None) -> str:
    """A timestamp a spreadsheet parses, in UTC.

    Seconds resolution and a space separator: microseconds and the `T` are what stop Excel and
    Sheets recognising the column as a date. The offset is dropped rather than rendered because a
    mixed-offset column sorts wrongly; everything here is UTC already.
    """
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def csv_response(
    filename: str,
    header: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> Response:
    """Build the download. ``rows`` may be a generator; it is consumed once."""
    buf = io.StringIO()
    buf.write(_BOM)
    writer = csv.writer(buf)
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(["" if cell is None else cell for cell in row])
    return Response(
        content=buf.getvalue(),
        # `charset=utf-8` so a browser previewing rather than downloading also decodes it right.
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
