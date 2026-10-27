"""A campaign's own call script, uploaded and read out as written (SDR feedback, 2026-10-26, D10).

The team calls from scripts they wrote; the console only ever showed an AI script generated from
scratch. Decided with the owner: **shown verbatim, with an AI side panel** (the pre-call brief: the
person, the company, the signals, each with its source), **and an option to enhance it**: a
suggested revision the rep accepts or rejects, the original kept.

* Text and Word (.docx) are read with the standard library. A .docx is a zip of XML; a document
  type declaration is refused before parsing, because a script never needs one and an entity in
  one is how an XML file expands without limit. PDF waits on adding a PDF reader.
* Only MARKED placeholders are filled, nothing else is touched: ``{{first_name}}``,
  ``{{full_name}}``, ``{{title}}``, ``{{company}}``, ``{{my_name}}`` and ``{{trigger}}`` (the
  strongest event a draft may cite). One with no value stays as written, so the rep sees the gap.
"""
from __future__ import annotations

import io
import re
import zipfile

MAX_BYTES = 2_000_000
MAX_CHARS = 20_000
#: A .docx's document.xml, uncompressed. A script is a few pages; this is hundreds.
MAX_XML_BYTES = 5_000_000

SUPPORTED = (".txt", ".docx")


class UnreadableScript(ValueError):
    """The file cannot be read as a script; the message says why, for the person who uploaded it."""


def read_document(filename: str, data: bytes) -> str:
    """The script's text, as written (line breaks kept), or `UnreadableScript`."""
    name = (filename or "").strip().lower()
    if len(data or b"") > MAX_BYTES:
        raise UnreadableScript("The file is over 2 MB. A call script is a few pages: upload the "
                               "text of it.")
    if not data:
        raise UnreadableScript("The file is empty.")
    if name.endswith(".txt"):
        text = _decode(data)
    elif name.endswith(".docx"):
        text = _docx_text(data)
    elif name.endswith(".pdf"):
        raise UnreadableScript("PDF is not supported yet. Save the script as Word (.docx) or "
                               "text (.txt) and upload that.")
    else:
        raise UnreadableScript("Upload the script as Word (.docx) or text (.txt).")
    text = _tidy(text)
    if not text:
        raise UnreadableScript("No text was found in the file.")
    if len(text) > MAX_CHARS:
        raise UnreadableScript(f"The script is over {MAX_CHARS:,} characters. Trim it to what "
                               "is said on a call.")
    return text


def _decode(data: bytes) -> str:
    """UTF-8 (with or without its mark), else Windows-1252, the way a file saved in Notepad or
    exported on Windows arrives (the same ladder as the CSV import)."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_text(data: bytes) -> str:
    from xml.etree import ElementTree

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_XML_BYTES:
                raise UnreadableScript("The document is too large to be a call script.")
            xml = archive.read(info)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise UnreadableScript("This is not a Word document that can be read. Save it as .docx "
                               "(not .doc) and try again.") from exc
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise UnreadableScript("This document cannot be read safely.")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise UnreadableScript("This Word document is damaged.") from exc
    lines: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t":
                parts.append(node.text or "")
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def _tidy(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    # More than one blank line in a row is layout, not meaning.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


PLACEHOLDERS = ("first_name", "full_name", "title", "company", "my_name", "trigger")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Fill the marked placeholders that have a value; leave everything else exactly as written."""
    def one(match: re.Match) -> str:
        key = match.group(1)
        value = (values.get(key) or "").strip() if key in PLACEHOLDERS else ""
        return value or match.group(0)

    return _PLACEHOLDER.sub(one, text or "")


ENHANCE_RULES = (
    "You improve a cold-call script an SDR team wrote. Keep its structure, its order and its "
    "voice; tighten wording, make each line easy to say out loud, and make the opener earn the "
    "next ten seconds. Never add a fact, a customer, a number or a claim the script does not "
    "already make. Keep every placeholder in double braces exactly as written. Answer with the "
    "revised script only, no commentary."
)


def enhance_prompt(script: str, product_context: str) -> str:
    context = (product_context or "").strip()
    return (ENHANCE_RULES + (f"\n\nWhat we sell (for context only):\n{context[:1500]}"
                             if context else "")
            + f"\n\nTHE SCRIPT:\n\"\"\"\n{script}\n\"\"\"")
