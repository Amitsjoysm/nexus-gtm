"""An export carries what the screen shows, and is never an unexplained empty file.

The account export wrote eight columns. The Accounts page shows a Fit score on every row, and
enrichment stores description, LinkedIn, sub-industry, revenue, city and keywords — all of which
the file dropped. Measured on the local `infojoy` workspace: 56 accounts, every one scored, 45 with
a description and 44 with a LinkedIn URL, and none of it in the export. A spreadsheet of names and
blank cells is what "the export is blank" looks like to the person who opened it.

The other half: a workspace with no contacts produced a header-only file with no message. That is
correct CSV and indistinguishable from a broken download. The row count now travels in a header so
the client can say "nothing to export" instead of saving an empty file.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from nexus.models.intelligence import AccountScore
from tests.conftest import auth, signup, tenant_session

BOM = "﻿"
ENRICHED = {
    "description": "Payments infrastructure for marketplaces.",
    "linkedin_url": "https://www.linkedin.com/company/globex",
    "sub_industry": "Payments",
    "revenue": "$10M-$50M",
    "city": "Austin",
    "region": "Texas",
    "keywords": ["payments", "marketplaces"],
}


def _parse(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.lstrip(BOM))))


async def _seed_enriched_account(client, token: str) -> str:
    r = await client.post("/api/accounts", headers=auth(token), json={
        "name": "Globex", "domain": "globex.com", "industry": "Fintech",
        "employee_count": 240, "country": "United States", "tech_stack": ["Stripe", "Segment"]})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    tid = (decode_access_token(token) or {})["tid"]
    async with tenant_session(tid) as ts:
        acct = await ts.get(Account, aid)
        acct.custom_fields = {**(acct.custom_fields or {}), **ENRICHED}
        ts.add(AccountScore(tenant_id=tid, account_id=aid, icp_fit=80, intent=70, health=60,
                            composite=77))
        await ts.flush()
    return aid


def _cell(value) -> str:
    """How the export renders a value the API returns: lists joined, None blank."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return str(value)


async def test_the_accounts_export_carries_the_fit_score_and_the_enrichment(client):
    token = await signup(client, slug="ec1", email="o@ec1.x", company="EC1")
    await _seed_enriched_account(client, token)

    rows = _parse((await client.get("/api/accounts/export/csv", headers=auth(token))).text)

    row = next(r for r in rows if r["name"] == "Globex")
    assert row["fit_score"] == "77"
    for field, value in ENRICHED.items():
        assert row[field] == _cell(value), f"{field} is on the screen but blank in the export"


async def test_every_account_column_agrees_with_what_the_list_shows(client):
    """One source of truth: a column present in both must hold the same value in both."""
    token = await signup(client, slug="ec2", email="o@ec2.x", company="EC2")
    aid = await _seed_enriched_account(client, token)

    listed = next(a for a in (await client.get("/api/accounts", headers=auth(token))).json()
                  if a["id"] == aid)
    row = next(r for r in _parse((await client.get("/api/accounts/export/csv",
                                                   headers=auth(token))).text)
               if r["name"] == "Globex")

    shared = [k for k in row if k in listed]
    assert len(shared) >= 15, shared
    for key in shared:
        assert row[key] == _cell(listed[key]), key


async def test_the_contacts_export_carries_what_the_contacts_table_shows(client):
    token = await signup(client, slug="ec3", email="o@ec3.x", company="EC3")
    tid = (decode_access_token(token) or {})["tid"]
    acct = (await client.post("/api/accounts", headers=auth(token),
                              json={"name": "Globex", "domain": "globex.com"})).json()
    from nexus.core.db import utcnow

    async with tenant_session(tid) as ts:
        ts.add(Contact(tenant_id=tid, account_id=acct["id"], full_name="Jane Peer",
                       title="VP of Sales", email="jane@globex.com", email_status="catch_all",
                       email_checked_at=utcnow(), custom_fields={"email_provider": "gsuite"},
                       linkedin_url="https://www.linkedin.com/in/jane-peer"))
        await ts.flush()

    row = _parse((await client.get("/api/contacts/export", headers=auth(token))).text)[0]

    # The table labels this "catch-all"; the file must not say something different.
    assert row["email_status"] == "catch-all"
    assert row["email_provider"] == "gsuite"
    assert row["email_checked_at"], "the Checked column is on screen and was missing from the file"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/jane-peer"


async def test_both_exports_report_how_many_rows_they_hold(client):
    token = await signup(client, slug="ec4", email="o@ec4.x", company="EC4")
    empty_accounts = await client.get("/api/accounts/export/csv", headers=auth(token))
    empty_contacts = await client.get("/api/contacts/export", headers=auth(token))
    assert empty_accounts.headers["x-row-count"] == "0"
    assert empty_contacts.headers["x-row-count"] == "0"

    await _seed_enriched_account(client, token)
    full = await client.get("/api/accounts/export/csv", headers=auth(token))
    assert full.headers["x-row-count"] == "1" == str(len(_parse(full.text)))


def test_the_client_says_nothing_to_export_instead_of_saving_an_empty_file():
    src = Path(__file__).resolve().parent.parent / "frontend" / "src"
    api = (src / "lib" / "api.ts").read_text(encoding="utf-8")
    download = api[api.index("private async download("):]
    download = download[: download.index("\n  }\n")]
    assert "X-Row-Count" in download, "download() must read the row count before saving"
    for rel in ("pages/AccountsPage.tsx", "pages/ContactsPage.tsx"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "Nothing to export" in text, f"{rel} saves a header-only file without saying why"
