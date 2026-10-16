"""The provider's rows, parsed into typed records.

Keys are matched without regard to case or punctuation: ``CompanyURL``, ``companyUrl`` and
``company_url`` are one field. Field names and casing are not a contract until the first real calls
are recorded (spec §11), and a casing change must read as the same data, not as a page of people
with no email. Each field lists the names it answers to; the first present wins.

``parse_page`` returns ``None`` for a body it cannot read, never an empty page: "the provider said
nothing matched" and "the provider said something we do not understand" need opposite responses,
and conflating them is how a broken integration looks like a quiet market.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_KEY = re.compile(r"[^a-z0-9]")


def _norm(key: str) -> str:
    return _KEY.sub("", str(key).lower())


def _index(item: dict) -> dict[str, object]:
    return {_norm(k): v for k, v in item.items()}


def _get(index: dict[str, object], *names: str) -> str:
    for name in names:
        value = index.get(name)
        if value is not None and value != "":
            return " ".join(str(value).split())
    return ""


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
        first_name=_get(i, "firstname"),
        last_name=_get(i, "lastname"),
        title=_get(i, "title", "jobtitle"),
        department=_get(i, "department"),
        seniority=_get(i, "senioritylevel", "seniority"),
        email=_get(i, "email", "emailaddress", "workemail"),
        direct_phone=_get(i, "directphone", "phone", "phonenumber"),
        company_name=_get(i, "companyname", "company"),
        company_url=_get(i, "companyurl", "companywebsite", "companydomain", "website"),
        country=_get(i, "country", "contactcountry"),
        state=_get(i, "state", "contactstate"),
        city=_get(i, "city", "contactcity"),
        zip=_get(i, "zipcode", "zip", "postalcode", "contactzipcode"),
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
        employee_size_range=_get(i, "employeesizerange", "employeesize", "employees"),
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
