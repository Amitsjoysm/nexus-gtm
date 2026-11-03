"""Sample import files: what a CSV for the account or contact import should look like.

Built from the field list ``/imports/fields`` serves, never from a hand-written copy: the column
headers ARE the mapping targets, so the mapping screen matches every one of them on sight, and a
field added to the import appears in the template the same day. ``GUIDE`` only says how each field
reads; a test refuses a mappable field that has no line here, so a new one cannot ship as a blank
column nobody can fill in.

The file is the header, one guide line (``Required: ...`` / ``Recommended: ...`` / ``Optional:
...`` under each column) and two example rows. Both are skipped by the importer, so a template
uploaded with the operator's rows added below it imports exactly those rows:

* the guide line is recognised by its shape (every filled cell opens with one of the three words
  and a colon), and only as the first data line;
* the examples are on ``example.com`` and ``example.org``, reserved for documentation (RFC 2606),
  which no real company or work address can be on.
"""
from __future__ import annotations

import csv as _csv
import io
import re
from dataclasses import dataclass

REQUIRED = "required"
RECOMMENDED = "recommended"
OPTIONAL = "optional"
_LEVEL_ORDER = {REQUIRED: 0, RECOMMENDED: 1, OPTIONAL: 2}
_LEVEL_WORD = {REQUIRED: "Required", RECOMMENDED: "Recommended", OPTIONAL: "Optional"}

#: Documentation domains (RFC 2606). The template's examples live here and the importer skips them.
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")

_GUIDE_CELL = re.compile(r"^\s*(required|recommended|optional)\s*:", re.I)


@dataclass(frozen=True, slots=True)
class FieldGuide:
    level: str
    note: str
    #: The two example rows' values for this column.
    examples: tuple[str, str] = ("", "")


#: Per entity, per mappable field: how it reads in the template. Keyed by entity because the same
#: name means different things on the two files (`linkedin_url` is a company page on an account,
#: a person's own profile on a contact).
GUIDE: dict[str, dict[str, FieldGuide]] = {
    "accounts": {
        "name": FieldGuide(REQUIRED, "the company's name. A website below is enough on its own.",
                           ("Example Analytics", "Example Logistics")),
        "domain": FieldGuide(REQUIRED, "the website, like acme.com. A company name is enough on "
                             "its own; with a website, the company you already have is updated.",
                             ("example.com", "example.org")),
        "industry": FieldGuide(RECOMMENDED, "the industry, in your words.",
                               ("Software Development", "Transportation and Logistics")),
        "country": FieldGuide(RECOMMENDED, "the country of the headquarters.",
                              ("United States", "United Kingdom")),
        "employee_count": FieldGuide(RECOMMENDED, "number of employees, a whole number.",
                                     ("250", "1200")),
        "region": FieldGuide(OPTIONAL, "state or province.", ("California", "England")),
        "postal_code": FieldGuide(OPTIONAL, "ZIP or postal code.", ("94105", "EC1A 1BB")),
        "annual_revenue": FieldGuide(OPTIONAL, "annual revenue, a whole number "
                                     "(25000000 or $25,000,000).", ("25000000", "")),
        "tech_stack": FieldGuide(OPTIONAL, "technologies they use, separated by commas or "
                                 "semicolons. Added to what is already known.",
                                 ("Salesforce; Snowflake", "HubSpot")),
        "crm_id": FieldGuide(OPTIONAL, "the record's id in your CRM, so a sync updates it instead "
                             "of making a second one.", ("", "0015g00000Abc12")),
        "crm_source": FieldGuide(OPTIONAL, "which CRM that id is from: hubspot or salesforce.",
                                 ("", "salesforce")),
    },
    "contacts": {
        "email": FieldGuide(REQUIRED, "the person's work email. It is how the contact is "
                            "matched, so a second import updates them.",
                            ("ada.lovelace@example.com", "grace.hopper@example.org")),
        "full_name": FieldGuide(RECOMMENDED, "first and last name.",
                                ("Ada Lovelace", "Grace Hopper")),
        "title": FieldGuide(RECOMMENDED, "job title.", ("VP Operations", "Head of Supply Chain")),
        "account_domain": FieldGuide(RECOMMENDED, "their company's website, like acme.com. Left "
                                     "blank, it is taken from the email.",
                                     ("example.com", "example.org")),
        "account_name": FieldGuide(OPTIONAL, "their company's name, used when the company is new "
                                   "to this workspace.", ("Example Analytics", "Example Logistics")),
        "seniority": FieldGuide(OPTIONAL, "seniority, like VP or Director.", ("VP", "Director")),
        "phone": FieldGuide(OPTIONAL, "phone number, with the country code.",
                            ("+1 415 555 0100", "+44 20 7946 0000")),
        "linkedin_url": FieldGuide(OPTIONAL, "their own LinkedIn profile, "
                                   "linkedin.com/in/their-name. Anything else is left blank.",
                                   ("", "")),
    },
}

#: How a column for one of the workspace's own fields reads.
_CUSTOM_NOTE = "your workspace's field “{label}”."


@dataclass(frozen=True, slots=True)
class Column:
    #: The header, which is also the mapping target the import screen matches it to.
    target: str
    level: str
    note: str
    examples: tuple[str, str]

    def guide_cell(self) -> str:
        return f"{_LEVEL_WORD[self.level]}: {self.note}"


def columns(entity: str, fields: list[str] | tuple[str, ...],
            custom: list[tuple[str, str]] = ()) -> list[Column]:
    """The template's columns: every built-in field, then the workspace's own ``(target, label)``
    fields, ordered required first. A field with no guide raises, which is what the test that
    covers every mappable field relies on."""
    guide = GUIDE[entity]
    out = [Column(f, guide[f].level, guide[f].note, guide[f].examples) for f in fields]
    out.sort(key=lambda c: _LEVEL_ORDER[c.level])  # stable: the served order within a level
    out += [Column(target, OPTIONAL, _CUSTOM_NOTE.format(label=label), ("", ""))
            for target, label in custom]
    return out


def required_notes(entity: str, fields: list[str] | tuple[str, ...]) -> list[dict]:
    """What the import screen says each required field needs, from the same guide."""
    return [{"field": c.target, "note": c.note}
            for c in columns(entity, fields) if c.level == REQUIRED]


def build_template(entity: str, fields: list[str] | tuple[str, ...],
                   custom: list[tuple[str, str]] = ()) -> str:
    """The CSV text: header, guide line, two example rows."""
    cols = columns(entity, fields, custom)
    buffer = io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([c.target for c in cols])
    writer.writerow([c.guide_cell() for c in cols])
    for i in range(2):
        writer.writerow([c.examples[i] for c in cols])
    return buffer.getvalue()


def is_guide_row(values) -> bool:
    """Is this the template's guide line? Every filled cell opens with Required:, Recommended: or
    Optional:, and at least two are filled, which no real company or person row does."""
    filled = [str(v) for v in values if isinstance(v, str) and v.strip()]
    return len(filled) >= 2 and all(_GUIDE_CELL.match(v) for v in filled)


def is_example_domain(domain: str) -> bool:
    """A domain reserved for documentation (or under one): the template's examples."""
    value = (domain or "").strip().lower().rstrip(".")
    return any(value == d or value.endswith("." + d) for d in EXAMPLE_DOMAINS)
