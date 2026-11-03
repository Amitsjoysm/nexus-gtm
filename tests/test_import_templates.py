"""Sample import files, and what is offered after an import (SDR round 2, 5.3).

The template is built from the field list `/imports/fields` serves, never a hand-written copy: its
headers ARE the mapping targets. Its guide line and its two example rows are skipped on import, so
a filled-in copy imports exactly the operator's rows. After an import, the screen offers to verify
the emails and fill the firmographics of the rows just written, through the product's existing
charged work, with the price shown first.
"""
from __future__ import annotations

import csv
import io
import json

import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


async def _template(client, h, entity: str) -> str:
    r = await client.get(f"/api/imports/template/{entity}.csv", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert f"{entity}-import-template.csv" in r.headers["content-disposition"]
    return r.text


async def _upload(client, h, entity: str, text: str, mapping: dict) -> dict:
    r = await client.post(
        f"/api/imports/{entity}/csv", headers=h,
        data={"mapping": json.dumps(mapping)},
        files={"file": ("list.csv", text.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---- the file ---------------------------------------------------------------------------------

def test_every_mappable_field_has_a_guide_line_and_no_stale_one():
    """A field added to the import without a line here would ship as a column nobody can fill."""
    from nexus.api.routers.imports import ACCOUNT_FIELDS, CONTACT_FIELDS
    from nexus.imports.template import GUIDE

    assert set(GUIDE["accounts"]) == set(ACCOUNT_FIELDS)
    assert set(GUIDE["contacts"]) == set(CONTACT_FIELDS)


@pytest.mark.parametrize("entity", ["accounts", "contacts"])
async def test_the_template_is_the_served_field_list_with_a_guide_and_two_examples(
    client, entity,
):
    h, _ = await _workspace(client, f"tpl-{entity[:3]}")
    fields = (await client.get("/api/imports/fields", headers=h)).json()
    served = fields["account_fields" if entity == "accounts" else "contact_fields"]

    rows = _parse(await _template(client, h, entity))
    header, guide, *examples = rows
    assert set(header) == set(served), "headers are the mapping targets, no more and no fewer"
    assert len(examples) == 2
    words = [cell.split(":", 1)[0] for cell in guide]
    assert set(words) <= {"Required", "Recommended", "Optional"}
    # Required columns come first, so the file opens on what matters.
    first_optional = words.index("Optional")
    assert "Required" not in words[first_optional:]

    # The screen's note for each required field is the same sentence the guide line carries.
    required = fields["account_required" if entity == "accounts" else "contact_required"]
    assert required, "every import has at least one required field"
    for item in required:
        cell = guide[header.index(item["field"])]
        assert cell == f"Required: {item['note']}"


async def test_the_workspace_s_own_fields_are_columns_too(client):
    from nexus.models.chat import CustomFieldDef

    h, tid = await _workspace(client, "tpl-cf")
    async with tenant_session(tid) as ts:
        ts.add(CustomFieldDef(tenant_id=tid, entity="account", key="territory",
                              label="Territory", kind="text"))
    header, guide, *_ = _parse(await _template(client, h, "accounts"))
    assert "custom:territory" in header
    assert guide[header.index("custom:territory")].startswith("Optional:")


async def test_another_entity_is_refused(client):
    h, _ = await _workspace(client, "tpl-x")
    r = await client.get("/api/imports/template/deals.csv", headers=h)
    assert r.status_code == 422


# ---- the round trip ---------------------------------------------------------------------------

async def test_the_template_uploaded_as_it_is_imports_nothing(client):
    from nexus.models.account import Account

    h, tid = await _workspace(client, "tpl-rt")
    text = await _template(client, h, "accounts")
    header = _parse(text)[0]
    result = await _upload(client, h, "accounts", text, {c: c for c in header})

    assert result["created"] == 0 and result["updated"] == 0
    # The guide line is not a row; the two examples are, and each says why it was skipped.
    assert result["total_rows"] == 2
    assert result["skipped"] == 2
    assert all("example row" in e for e in result["errors"])
    async with tenant_session(tid) as ts:
        assert await ts.list(Account) == []


async def test_a_filled_in_template_imports_only_the_operator_s_rows(client):
    h, tid = await _workspace(client, "tpl-fill")
    text = await _template(client, h, "contacts")
    rows = _parse(text)
    header = rows[0]
    mine = {c: "" for c in header}
    mine.update({"email": "sam@globex.io", "full_name": "Sam Porter", "title": "COO"})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    writer.writerow([mine[c] for c in header])

    result = await _upload(client, h, "contacts", buffer.getvalue(), {c: c for c in header})
    assert result["created"] == 1
    assert result["skipped"] == 2
    assert len(result["contact_ids"]) == 1


async def test_a_real_first_row_is_never_mistaken_for_the_guide_line():
    """Only a row whose every filled cell opens with Required:/Recommended:/Optional: is the guide;
    a company called "Optional Systems" is a company."""
    from nexus.imports.template import is_guide_row

    assert not is_guide_row(["Optional Systems", "optional.io"])
    assert not is_guide_row(["Required: one cell only"])
    assert is_guide_row(["Required: the name", "", "Optional: the region"])


# ---- after the import ---------------------------------------------------------------------------

@pytest.fixture
def verdicts(monkeypatch):
    from nexus.verification import EmailVerification

    def fresh():
        async def verify(email: str):
            return EmailVerification(email=email, status="valid", confidence=0.9,
                                     source="reacher")
        return verify

    monkeypatch.setattr("nexus.enrichment.reverify.fresh_verify", fresh)


@pytest.fixture
def queued(monkeypatch):
    jobs: list[dict] = []

    async def fake(tenant_id, user_id, account_ids, *, queue=None):
        jobs.append({"tenant_id": tenant_id, "account_ids": list(account_ids)})

    import nexus.workers.tasks as tasks

    monkeypatch.setattr(tasks, "enqueue_fill_firmographics", fake)
    return jobs


async def _usage(tid: str, capability: str) -> float:
    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        rows = await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id == capability)
    return sum(float(e.quantity) for e in rows)


CONTACTS_CSV = (
    "email,full_name\n"
    "a@initech.io,Ann Able\n"
    "b@initech.io,Ben Best\n"
    "c@initech.io,Cy Cole\n"
)


async def test_the_price_is_shown_first_and_nothing_is_charged_for_it(client):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_rates()
    h, tid = await _workspace(client, "act-q")
    result = await _upload(client, h, "contacts", CONTACTS_CSV,
                           {"email": "email", "full_name": "full_name"})
    ids = result["contact_ids"]
    assert len(ids) == 3

    r = await client.post("/api/imports/actions/quote", headers=h,
                          json={"action": "verify_emails", "ids": ids})
    assert r.status_code == 200, r.text
    quote = r.json()
    assert quote["count"] == 3 and quote["charged"] == 3
    assert quote["credits_per_row"] > 0
    assert quote["total_credits"] == pytest.approx(quote["credits_per_row"] * 3)
    assert await _usage(tid, "verify.email") == 0


async def test_verify_emails_checks_the_imported_addresses_and_charges_as_verify_does(
    client, verdicts,
):
    from nexus.models.account import Contact

    h, tid = await _workspace(client, "act-v")
    result = await _upload(client, h, "contacts", CONTACTS_CSV,
                           {"email": "email", "full_name": "full_name"})
    r = await client.post("/api/imports/actions/verify-emails", headers=h,
                          json={"ids": result["contact_ids"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"action": "verify_emails", "mode": "inline", "count": 3, "charged": 3}

    async with tenant_session(tid) as ts:
        assert {c.email_status for c in await ts.list(Contact)} == {"valid"}
    assert await _usage(tid, "verify.email") == 3

    # Asked again, nothing is left to verify, and nothing more is charged.
    again = await client.post("/api/imports/actions/verify-emails", headers=h,
                              json={"ids": result["contact_ids"]})
    assert again.json()["count"] == 0
    assert await _usage(tid, "verify.email") == 3


async def test_fill_firmographics_queues_only_the_accounts_missing_some(client, queued):
    h, tid = await _workspace(client, "act-f")
    text = ("name,domain,industry,employee_count,country\n"
            "Initech,initech.io,Software,120,United States\n"
            "Globex,globex.io,,,\n"
            "Hooli,hooli.io,Software,,\n")
    result = await _upload(client, h, "accounts", text, {
        "name": "name", "domain": "domain", "industry": "industry",
        "employee_count": "employee_count", "country": "country"})
    ids = result["account_ids"]
    assert len(ids) == 3

    r = await client.post("/api/imports/actions/fill-firmographics", headers=h, json={"ids": ids})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "queued" and r.json()["count"] == 2
    assert len(queued) == 1 and len(queued[0]["account_ids"]) == 2


async def test_an_unaffordable_action_is_refused_before_anything_runs(client, queued, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    h, tid = await _workspace(client, "act-402")
    result = await _upload(client, h, "accounts", "name,domain\nGlobex,globex.io\n",
                           {"name": "name", "domain": "domain"})
    from nexus.billing.credits import balance, burn_credits

    async with tenant_session(tid) as ts:
        held = float(await balance(ts))
        if held > 0:
            await burn_credits(ts, held, capability_id="test.drain", idempotency_key="drain")
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")

    quote = (await client.post("/api/imports/actions/quote", headers=h, json={
        "action": "fill_firmographics", "ids": result["account_ids"]})).json()
    assert quote["allowed"] is False and quote["reason"]

    r = await client.post("/api/imports/actions/fill-firmographics", headers=h,
                          json={"ids": result["account_ids"]})
    assert r.status_code == 402, r.text
    assert queued == []


async def test_another_workspace_s_rows_are_not_acted_on(client, queued):
    h1, _ = await _workspace(client, "act-t1")
    h2, _ = await _workspace(client, "act-t2")
    result = await _upload(client, h1, "accounts", "name,domain\nGlobex,globex.io\n",
                           {"name": "name", "domain": "domain"})
    r = await client.post("/api/imports/actions/fill-firmographics", headers=h2,
                          json={"ids": result["account_ids"]})
    assert r.status_code == 200 and r.json()["count"] == 0
    assert queued == []
