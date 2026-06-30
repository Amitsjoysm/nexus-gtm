# tests/test_network_linkedin_csv.py
from __future__ import annotations


def test_parse_linkedin_export_with_preamble():
    from nexus.network.linkedin_csv import parse_linkedin_csv

    raw = (
        "Notes:\n"
        '"When exporting your connection data, you may notice that...":\n'
        "\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Okafor,https://www.linkedin.com/in/ada,ada@helix.com,Helix Health,CTO,01 Jun 2026\n"
        "Bob,Roy,https://www.linkedin.com/in/bobroy,,Nimbus Rx,VP Engineering,15 May 2026\n"
    )
    rows = parse_linkedin_csv(raw.encode("utf-8"))
    assert len(rows) == 2
    ada = rows[0]
    assert ada.external_id == "https://www.linkedin.com/in/ada"
    assert ada.email == "ada@helix.com"
    assert ada.name == "Ada Okafor"
    assert ada.title == "CTO"
    assert ada.company == "Helix Health"
    assert ada.relation == "linkedin_1st"
    # second row has no email but still imports (external_id from the profile URL)
    assert rows[1].email is None
    assert rows[1].name == "Bob Roy"


def test_parse_rejects_a_non_linkedin_csv():
    from nexus.network.linkedin_csv import LinkedInCsvError, parse_linkedin_csv

    try:
        parse_linkedin_csv(b"foo,bar\n1,2\n")
    except LinkedInCsvError:
        return
    raise AssertionError("expected LinkedInCsvError for a CSV without LinkedIn headers")
