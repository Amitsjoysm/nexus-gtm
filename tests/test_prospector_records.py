"""What the provider returned, and who each row is.

The parser is tolerant of casing (``CompanyURL`` == ``companyUrl`` == ``company_url``): field names
are not a contract until the first real calls are recorded, and a casing change must not turn every
email into an empty string. Identity follows the shared stores' rules: a person is their normalised
email, else first and last name at the company's domain; a company is its normalised domain. Never a
bare name, which is how this codebase shipped six wrong-attribution bugs.
"""
from __future__ import annotations

import hashlib

from tests.prospector_fixtures import COMPANIES, CONTACTS, page


def test_a_documented_page_parses():
    from nexus.prospector.records import parse_contact, parse_page

    parsed = parse_page(page(CONTACTS, total=4))
    assert parsed is not None and parsed.total == 4 and len(parsed.items) == 4
    john = parse_contact(parsed.items[0])
    assert (john.first_name, john.last_name, john.full_name) == ("John", "Smith", "John Smith")
    assert john.email == "john.smith@acme-example.com"
    assert john.direct_phone == "+1 (415) 555-7890"
    assert (john.title, john.department, john.seniority) == ("VP Sales", "Sales", "VP")
    assert john.company_url == "acme-example.com" and john.country == "United States"


def test_casing_and_punctuation_in_keys_do_not_matter():
    from nexus.prospector.records import parse_company, parse_contact

    camel = {"firstName": "Ana", "lastName": "Okafor", "email": "a@x-example.com",
             "companyUrl": "x-example.com", "seniority_level": "Manager"}
    c = parse_contact(camel)
    assert (c.first_name, c.email, c.company_url, c.seniority) == \
        ("Ana", "a@x-example.com", "x-example.com", "Manager")
    co = parse_company({"company_name": "X", "companyLinkedin": "https://linkedin.com/company/x",
                        "industryType": "Software", "employeeSizeRange": "11-50"})
    assert (co.linkedin_url, co.industry, co.employee_size_range) == \
        ("https://linkedin.com/company/x", "Software", "11-50")


def test_missing_fields_read_as_empty_and_values_are_trimmed():
    from nexus.prospector.records import parse_contact

    c = parse_contact({"FirstName": "  Wei ", "Email": None})
    assert c.first_name == "Wei" and c.email == "" and c.title == ""


def test_the_total_is_read_whatever_its_name():
    from nexus.prospector.records import parse_page

    assert parse_page({"Items": [], "totalCount": 9}).total == 9
    assert parse_page({"Items": [], "Total": 3}).total == 3
    assert parse_page({"Items": []}).total is None


def test_an_unparseable_body_is_none_not_an_empty_page():
    from nexus.prospector.records import parse_page

    assert parse_page([1, 2]) is None
    assert parse_page({"Errors": ["PageSize cannot be greater than 500."]}) is None
    assert parse_page({"Items": "nope"}) is None


def test_a_person_is_their_email_whatever_its_case():
    from nexus.prospector.identity import contact_identity
    from nexus.prospector.records import parse_contact

    a = parse_contact({**CONTACTS[0], "Email": "John.Smith@ACME-example.com"})
    b = parse_contact(CONTACTS[0])
    assert contact_identity(a) == contact_identity(b) == "e:john.smith@acme-example.com"


def test_without_an_email_a_person_is_their_name_at_the_companys_domain():
    from nexus.prospector.identity import contact_identity
    from nexus.prospector.records import parse_contact

    wei = parse_contact(CONTACTS[2])
    assert contact_identity(wei) == "n:wei lindqvist|initech-example.co.uk"


def test_a_person_with_neither_has_no_identity():
    from nexus.prospector.identity import contact_identity
    from nexus.prospector.records import parse_contact

    assert contact_identity(parse_contact({"FirstName": "Solo"})) == ""


def test_a_company_is_its_domain_else_its_normalised_name():
    from nexus.prospector.identity import company_identity
    from nexus.prospector.records import parse_company

    assert company_identity(parse_company(COMPANIES[2])) == "d:initech-example.co.uk"
    nameless = parse_company({"CompanyName": "Hooli, Inc.", "CompanyURL": ""})
    assert company_identity(nameless) == "n:hooli"


def test_row_keys_are_tenant_bound_and_never_a_bare_hash_of_the_email():
    from nexus.prospector.identity import fingerprint, row_key

    ident = "e:john.smith@acme-example.com"
    a, b = row_key("tenant-a", ident), row_key("tenant-b", ident)
    assert a != b and len(a) == 32
    assert a == row_key("tenant-a", ident)
    bare = hashlib.sha256(b"john.smith@acme-example.com").hexdigest()
    assert bare[:32] != a and fingerprint(ident) != a
    assert len(fingerprint(ident)) == 64
