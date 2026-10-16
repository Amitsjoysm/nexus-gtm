"""Synthetic InfoJoy Search API answers for Prospector's tests.

Every name, address, number and domain here is invented (example-style domains); no real person's
data is in the suite. The shape follows the provider's documented samples (PascalCase keys,
``{"PageNumber", "PageSize", "Items"}``) until the first real calls are recorded in
``docs/superpowers/specs/2026-10-16-prospector-observed-api.md``.

``FakeVendor`` is an ``httpx.MockTransport`` handler that serves contacts or companies by page, can
answer with a status instead, and records every request, so a test can assert what was sent and how
many times the vendor was asked.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

CONTACTS = [
    {"Title": "VP Sales", "FirstName": "John", "LastName": "Smith",
     "Email": "john.smith@acme-example.com", "DirectPhone": "+1 (415) 555-7890",
     "Department": "Sales", "SeniorityLevel": "VP", "CompanyName": "Acme Example Inc",
     "CompanyURL": "acme-example.com", "Country": "United States"},
    {"Title": "Head of Revenue Operations", "FirstName": "Priya", "LastName": "Raman",
     "Email": "priya@globex-example.io", "DirectPhone": "", "Department": "Operations",
     "SeniorityLevel": "Director", "CompanyName": "Globex Example",
     "CompanyURL": "www.globex-example.io", "Country": "United States"},
    {"Title": "Chief Financial Officer", "FirstName": "Wei", "LastName": "Lindqvist",
     "Email": "", "DirectPhone": "+44 20 7946 0123", "Department": "Finance",
     "SeniorityLevel": "C-Level", "CompanyName": "Initech Example",
     "CompanyURL": "https://initech-example.co.uk/", "Country": "United Kingdom"},
    {"Title": "Sales Manager", "FirstName": "Ana", "LastName": "Okafor",
     "Email": "ana.okafor@umbrella-example.net", "DirectPhone": "(212) 555-0147",
     "Department": "Sales", "SeniorityLevel": "Manager", "CompanyName": "Umbrella Example",
     "CompanyURL": "umbrella-example.net", "Country": "United States"},
]

COMPANIES = [
    {"CompanyName": "Acme Example Inc", "CompanyURL": "acme-example.com",
     "Country": "United States", "State": "CA", "City": "San Francisco",
     "IndustryType": "Software", "EmployeeSizeRange": "51-200",
     "CompanyLinkedin": "https://linkedin.com/company/acme-example"},
    {"CompanyName": "Globex Example", "CompanyURL": "globex-example.io",
     "Country": "United States", "State": "NY", "City": "New York",
     "IndustryType": "Financial Services", "EmployeeSizeRange": "201-500",
     "CompanyLinkedin": "https://www.linkedin.com/company/globex-example/"},
    {"CompanyName": "Initech Example", "CompanyURL": "https://initech-example.co.uk",
     "Country": "United Kingdom", "State": "", "City": "London",
     "IndustryType": "Software", "EmployeeSizeRange": "11-50", "CompanyLinkedin": ""},
]

#: Every person-identifying or paid value in the fixtures: none may appear in a masked response.
SECRETS = [
    *(c["Email"] for c in CONTACTS if c["Email"]),
    *(c["DirectPhone"] for c in CONTACTS if c["DirectPhone"]),
    *(c["CompanyLinkedin"] for c in COMPANIES if c["CompanyLinkedin"]),
    "acme-example", "globex-example", "initech-example", "umbrella-example",
    "john.smith", "ana.okafor", "priya@", "5557890", "79460123", "5550147",
]


def page(items: list[dict], *, number: int = 1, size: int = 25, total: int | None = None) -> dict:
    body = {"PageNumber": number, "PageSize": size, "Items": items}
    if total is not None:
        body["TotalCount"] = total
    return body


@dataclass
class FakeVendor:
    """Serves ``rows`` in pages of the requested size. ``status`` overrides with an error."""

    contacts: list[dict] = field(default_factory=lambda: list(CONTACTS))
    companies: list[dict] = field(default_factory=lambda: list(COMPANIES))
    status: int = 200
    error_body: dict | None = None
    requests: list[dict] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"path": request.url.path, "body": body,
                              "token": request.headers.get("X-Api-Token")})
        if self.status != 200:
            return httpx.Response(self.status, json=self.error_body or {"error": "nope"})
        rows = self.contacts if request.url.path.endswith("/contacts") else self.companies
        number = max(1, int(body.get("PageNumber") or 1))
        size = int(body.get("PageSize") or 25)
        chunk = rows[(number - 1) * size: number * size]
        total = len(rows) if body.get("IncludeTotalCount") else None
        return httpx.Response(200, json=page(chunk, number=number, size=size, total=total))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def many_contacts(n: int, *, prefix: str = "p") -> list[dict]:
    """``n`` distinct synthetic contacts, for paging and scan tests."""
    return [{"Title": "Account Executive", "FirstName": f"{prefix}{i}", "LastName": "Example",
             "Email": f"{prefix}{i}@bulk-example.com", "DirectPhone": f"+1 415 555 {1000 + i:04d}",
             "Department": "Sales", "SeniorityLevel": "Staff", "CompanyName": f"Bulk {i} Example",
             "CompanyURL": f"bulk{i}-example.com", "Country": "United States"} for i in range(n)]
