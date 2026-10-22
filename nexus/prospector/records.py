"""The provider's rows, parsed into typed records.

Keys are matched without regard to case or punctuation: ``CompanyURL``, ``companyUrl`` and
``company_url`` are one field. Field names and casing are not a contract until the first real calls
are recorded (spec §11), and a casing change must read as the same data, not as a page of people
with no email. Each field lists the names it answers to; the first present wins.

``parse_page`` returns ``None`` for a body it cannot read, never an empty page: "the provider said
nothing matched" and "the provider said something we do not understand" need opposite responses,
and conflating them is how a broken integration looks like a quiet market.

Observed 2026-10-22, the first real answers (docs/superpowers/specs/2026-10-16-prospector-observed-
api.md): a contact carries BOTH ``DirectPhone`` and ``Phone``. ``Phone`` sits beside ``Extension`` and
``Fax`` and is the office line: in 50 rows ``DirectPhone`` was filled 6 times and ``Phone`` 35, and the
4 rows holding both held different numbers, while the provider's own ``HasDirectPhone`` filter keys
on ``DirectPhone`` alone. So only ``DirectPhone`` is a direct phone: reading ``Phone`` as one sold an
office switchboard as a direct dial, and charged for it. The person's own profile is
``EmployeeLinkedIn`` (26 of 50 filled, every one a ``/in/`` URL).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_KEY = re.compile(r"[^a-z0-9]")


def _norm(key: str) -> str:
    return _KEY.sub("", str(key).lower())


def _index(item: dict) -> dict[str, object]:
    return {_norm(k): v for k, v in item.items()}


#: Words the provider stores for "no value" (observed: "NULL" in company State and City, "Not
#: Available" in IndustryType and EmployeeSizeRange). Shown, they read as a real place or industry.
_NO_VALUE = frozenset({"null", "not available", "n/a", "na", "none", "-"})


def _get(index: dict[str, object], *names: str, placeholders: bool = True) -> str:
    """The first present value. ``placeholders=False`` keeps words like "None", for names."""
    for name in names:
        value = index.get(name)
        if value is None or value == "":
            continue
        text = " ".join(str(value).split())
        if text and (not placeholders or text.lower() not in _NO_VALUE):
            return text
    return ""


def _size(value: str) -> str:
    """"1-10 employees" and " 1-10" are the "1-10" bucket: rows say "N employees" themselves."""
    text = value.strip()
    if text.lower().endswith(" employees"):
        text = text[: -len(" employees")].strip()
    return text


@dataclass(frozen=True, slots=True)
class VendorContact:
    first_name: str
    last_name: str
    title: str
    department: str
    seniority: str
    email: str
    direct_phone: str
    company_name: str
    company_url: str
    country: str
    state: str = ""
    city: str = ""
    zip: str = ""
    #: The person's own LinkedIn profile, as sent. Masked until paid; checked to be a ``/in/``
    #: profile (``enrichment.linkedin.person_profile_url``) before it is written anywhere.
    linkedin_url: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass(frozen=True, slots=True)
class VendorCompany:
    company_name: str
    company_url: str
    country: str
    state: str
    city: str
    industry: str
    employee_size_range: str
    linkedin_url: str
    zip: str = ""


@dataclass(slots=True)
class ParsedPage:
    #: Raw item dicts, as the provider sent them: this is what the cache seals.
    items: list[dict]
    total: int | None = None


def parse_contact(item: dict) -> VendorContact:
    i = _index(item)
    return VendorContact(
        first_name=_get(i, "firstname", placeholders=False),
        last_name=_get(i, "lastname", placeholders=False),
        title=_get(i, "title", "jobtitle"),
        department=_get(i, "department"),
        seniority=_get(i, "senioritylevel", "seniority"),
        email=_get(i, "email", "emailaddress", "workemail"),
        # DirectPhone only: the contact's ``Phone`` is the office line (see the module docstring).
        direct_phone=_get(i, "directphone", "directdial", "mobilephone"),
        company_name=_get(i, "companyname", "company"),
        company_url=_get(i, "companyurl", "companywebsite", "companydomain", "website"),
        country=_get(i, "country", "contactcountry"),
        state=_get(i, "state", "contactstate"),
        city=_get(i, "city", "contactcity"),
        zip=_get(i, "zipcode", "zip", "postalcode", "contactzipcode"),
        linkedin_url=_get(i, "employeelinkedin", "personlinkedin", "contactlinkedin"),
    )


def parse_company(item: dict) -> VendorCompany:
    i = _index(item)
    return VendorCompany(
        company_name=_get(i, "companyname", "name"),
        company_url=_get(i, "companyurl", "website", "domain", "companydomain"),
        country=_get(i, "country", "companycountry"),
        state=_get(i, "state", "companystate"),
        city=_get(i, "city", "companycity"),
        industry=_get(i, "industrytype", "industry"),
        employee_size_range=_size(_get(i, "employeesizerange", "employeesize", "employees")),
        linkedin_url=_get(i, "companylinkedin", "linkedinurl", "linkedin"),
        zip=_get(i, "zipcode", "zip", "postalcode"),
    )


def parse_page(body: object) -> ParsedPage | None:
    if not isinstance(body, dict):
        return None
    index = _index(body)
    items = index.get("items")
    if not isinstance(items, list):
        return None
    total = None
    for name in ("totalcount", "total", "totalrecords", "totalitems"):
        value = index.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            total = value
            break
    return ParsedPage(items=[i for i in items if isinstance(i, dict)], total=total)
