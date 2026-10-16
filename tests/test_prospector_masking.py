"""Partial masks (decision D7): enough to judge a row, never enough to use it unpaid.

The rule the property test pins: a mask never contains three consecutive characters of the
address's local part, its domain label or a LinkedIn slug, nor the first six digits of a phone. The
top-level domain and the last four digits of a phone are the only fragments shown, and only when
the value is long enough that they reveal little.
"""
from __future__ import annotations

import re

import pytest

from tests.prospector_fixtures import COMPANIES, CONTACTS


@pytest.mark.parametrize("raw, masked", [
    ("john.smith@example.com", "j•••@e•••.com"),
    ("a@b.co", "•••@•••.co"),
    ("Priya@Globex-Example.io", "p•••@g•••.io"),
    ("", ""),
    ("not-an-email", "•••"),
])
def test_email_masks(raw, masked):
    from nexus.prospector.masking import mask_email

    assert mask_email(raw) == masked


@pytest.mark.parametrize("raw, masked", [
    ("+1 (415) 555-7890", "•••-•••-7890"),
    ("12345", "•••-•••-••••"),
    ("", ""),
])
def test_phone_masks(raw, masked):
    from nexus.prospector.masking import mask_phone

    assert mask_phone(raw) == masked


@pytest.mark.parametrize("raw, masked", [
    ("example.com", "e•••.com"),
    ("https://www.initech-example.co.uk/about", "i•••.uk"),
    ("x.io", "•••.io"),
    ("", ""),
])
def test_domain_masks(raw, masked):
    from nexus.prospector.masking import mask_domain

    assert mask_domain(raw) == masked


@pytest.mark.parametrize("raw, masked", [
    ("https://linkedin.com/company/example-inc", "linkedin.com/company/e•••"),
    ("https://www.linkedin.com/company/globex-example/", "linkedin.com/company/g•••"),
    ("https://www.linkedin.com/in/john-smith-123", "linkedin.com/in/j•••"),
    ("", ""),
])
def test_linkedin_masks(raw, masked):
    from nexus.prospector.masking import mask_linkedin

    assert mask_linkedin(raw) == masked


def _fragments(value: str, n: int = 3) -> set[str]:
    value = value.lower()
    return {value[i:i + n] for i in range(len(value) - n + 1)}


def test_no_mask_carries_three_characters_of_what_it_hides():
    from nexus.prospector.masking import mask_domain, mask_email, mask_linkedin, mask_phone

    for contact in CONTACTS:
        email = contact["Email"]
        if email:
            local, domain = email.lower().split("@")
            mask = mask_email(email).lower()
            for part in (local, domain.split(".")[0]):
                assert not (_fragments(part) & _fragments(mask)), (email, mask)
        phone = re.sub(r"\D", "", contact["DirectPhone"])
        if phone:
            assert phone[:6] not in re.sub(r"\D", "", mask_phone(contact["DirectPhone"]))
        url = contact["CompanyURL"]
        label = re.sub(r"^(https?://)?(www\.)?", "", url).split(".")[0]
        assert not (_fragments(label) & _fragments(mask_domain(url).lower())), url
    for company in COMPANIES:
        if company["CompanyLinkedin"]:
            slug = company["CompanyLinkedin"].rstrip("/").rsplit("/", 1)[-1]
            mask = mask_linkedin(company["CompanyLinkedin"])
            assert not (_fragments(slug) & _fragments(mask.rsplit("/", 1)[-1])), mask
