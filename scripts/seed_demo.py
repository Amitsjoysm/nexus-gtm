"""Seed a demo NEXUS workspace against a running API, for local testing.

Usage:
    python scripts/seed_demo.py [--base-url http://127.0.0.1:9000]

Creates (idempotently) a demo workspace owner, a set of accounts, runs the
enrichment/scoring pipeline on each (which generates contacts, signals, inbox
tasks and alerts), and invites a couple of teammates. Prints the login
credentials and a summary of what was created.

Safe to re-run: if the owner already exists it logs in instead of signing up,
and accounts are matched by name so they aren't duplicated.
"""
from __future__ import annotations

import argparse
import sys

import httpx

OWNER = {
    "company_name": "Northwind GTM",
    "company_slug": "northwind",
    "email": "owner@northwind.example",
    "full_name": "Dana Okafor",
    "password": "demo-password-123",
}

TEAMMATES = [
    {"full_name": "Marco Reyes", "email": "marco@northwind.example",
     "password": "demo-password-123", "role": "manager"},
    {"full_name": "Priya Nair", "email": "priya@northwind.example",
     "password": "demo-password-123", "role": "rep"},
]

# Plays must exist BEFORE the pipeline runs: the engine only evaluates plays
# against newly-ingested signals. The demo source emits a strength-0.8 "funding"
# signal per account (>=100 employees), so this play fires one alert per account.
PLAYS = [
    {
        "name": "Funding alert",
        "enabled": True,
        "trigger": {"signal_kinds": ["funding"], "min_strength": 0.5},
        "actions": [
            {"type": "alert", "message": "Account announced new funding",
             "body": "A funding signal crossed the threshold — reach out now.",
             "severity": "critical", "channel": "in_app"},
        ],
    },
    {
        "name": "Hiring watch",
        "enabled": True,
        "trigger": {"signal_kinds": ["job_posting"], "min_strength": 0.0},
        "actions": [
            {"type": "alert", "message": "Relevant hiring detected",
             "body": "New job posting suggests an active initiative.",
             "severity": "warning", "channel": "in_app"},
        ],
    },
]

ACCOUNTS = [
    {"name": "Acme Robotics", "domain": "acmerobotics.example", "industry": "Manufacturing",
     "employee_count": 1200, "country": "United States",
     "tech_stack": ["Snowflake", "Segment", "Salesforce"]},
    {"name": "Helios Energy", "domain": "helios.example", "industry": "Clean Energy",
     "employee_count": 340, "country": "Germany",
     "tech_stack": ["Databricks", "HubSpot", "AWS"]},
    {"name": "Vertex Health", "domain": "vertexhealth.example", "industry": "Healthcare",
     "employee_count": 5400, "country": "United States",
     "tech_stack": ["Epic", "Azure", "Marketo"]},
    {"name": "Lumen Retail", "domain": "lumenretail.example", "industry": "Retail",
     "employee_count": 820, "country": "United Kingdom",
     "tech_stack": ["Shopify", "Klaviyo", "GCP"]},
    {"name": "Cobalt Fintech", "domain": "cobalt.example", "industry": "Financial Services",
     "employee_count": 210, "country": "Singapore",
     "tech_stack": ["Snowflake", "Looker", "Stripe"]},
]


# The tenant's Relevance Engine profile (ICP + value props + product context). Without this the
# fit score has nothing to score against — every account lands on the same neutral number. It is
# set BEFORE the pipeline runs so the accounts score correctly on their first pass. Chosen to give
# the demo accounts a realistic spread (in-band FinServ/Manufacturing high; wrong-geo/-industry low).
RELEVANCE_PROFILE = {
    "icp": {
        "industries": ["Financial Services", "Manufacturing", "Retail", "Healthcare",
                        "SaaS", "Software"],
        "employee_min": 200,
        "employee_max": 2000,
        "countries": ["United States", "United Kingdom", "Singapore"],
        "required_tech": ["Snowflake", "Salesforce", "Segment", "Stripe", "Looker"],
    },
    "value_props": [
        {"name": "Faster GTM",
         "description": "Turn buying signals into pipeline before competitors notice.",
         "pains_solved": ["slow pipeline", "missed intent"]},
        {"name": "ICP focus",
         "description": "Score every account against your ICP automatically.",
         "pains_solved": ["wasted rep time", "low win rate"]},
    ],
    "product_context": "NEXUS GTM — AI go-to-market intelligence for B2B revenue teams.",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    args = parser.parse_args()

    api = args.base_url.rstrip("/") + "/api"
    client = httpx.Client(base_url=api, timeout=30.0)

    # 1) Sign up the owner, or log in if they already exist.
    token = signup_or_login(client)
    auth = {"Authorization": f"Bearer {token}"}

    # 2) Set the Relevance Engine profile (ICP) BEFORE scoring, so fit scores are meaningful.
    r = client.put("/relevance/profile", headers=auth, json=RELEVANCE_PROFILE)
    if r.status_code >= 400:
        print(f"  ! relevance profile failed: {r.status_code} {r.text}", file=sys.stderr)

    # 3) Ensure alert-generating plays exist BEFORE any pipeline runs.
    existing_plays = {p["name"] for p in client.get("/plays", headers=auth).json()}
    for play in PLAYS:
        if play["name"] in existing_plays:
            continue
        r = client.post("/plays", headers=auth, json=play)
        if r.status_code >= 400:
            print(f"  ! play '{play['name']}' failed: {r.status_code} {r.text}", file=sys.stderr)

    # 4) Ensure accounts exist (match by name to stay idempotent).
    existing = {a["name"]: a for a in client.get("/accounts", headers=auth).json()}
    created = 0
    account_ids = []
    for acc in ACCOUNTS:
        found = existing.get(acc["name"])
        if found:
            account_ids.append(found["id"])
            continue
        r = client.post("/accounts", headers=auth, json=acc)
        r.raise_for_status()
        account_ids.append(r.json()["id"])
        created += 1

    # 5) Run the pipeline on each account (enrich + score + generate signals/tasks/alerts).
    for aid in account_ids:
        r = client.post(f"/agents/pipeline/{aid}", headers=auth)
        if r.status_code >= 400:
            print(f"  ! pipeline failed for {aid}: {r.status_code} {r.text}", file=sys.stderr)

    # 6) Invite teammates (ignore conflicts on re-run).
    invited = 0
    for mate in TEAMMATES:
        r = client.post("/workspace/members", headers=auth, json=mate)
        if r.status_code < 400:
            invited += 1
        elif r.status_code not in (400, 409):
            print(f"  ! invite failed for {mate['email']}: {r.status_code} {r.text}",
                  file=sys.stderr)

    # 7) Summarize.
    overview = client.get("/analytics/overview", headers=auth).json()
    inbox = client.get("/inbox", headers=auth).json()
    signals = client.get("/signals", headers=auth).json()
    alerts = client.get("/alerts", headers=auth, params={"status": "open"}).json()

    print("\n=== NEXUS demo data seeded ===")
    print(f"  API:            {api}")
    print(f"  Accounts:       {len(account_ids)} ({created} new)")
    print(f"  Teammates:      +{invited} invited")
    print(f"  Inbox tasks:    {len(inbox)}")
    print(f"  Signals:        {len(signals)}")
    print(f"  Open alerts:    {len(alerts)}")
    print(f"  Analytics keys: {', '.join(overview) or '(none)'}")
    print("\n  Log in at the frontend with:")
    print(f"    Email:     {OWNER['email']}")
    print(f"    Password:  {OWNER['password']}")
    print(f"    Workspace: {OWNER['company_slug']} (only needed if prompted)")
    return 0


def signup_or_login(client: httpx.Client) -> str:
    r = client.post("/auth/signup", json=OWNER)
    if r.status_code < 400:
        print(f"Created workspace owner {OWNER['email']}")
        return r.json()["access_token"]

    # Already registered — log in instead.
    r = client.post(
        "/auth/login",
        json={"email": OWNER["email"], "password": OWNER["password"],
              "tenant_slug": OWNER["company_slug"]},
    )
    if r.status_code >= 400:
        raise SystemExit(f"signup and login both failed: {r.status_code} {r.text}")
    print(f"Owner {OWNER['email']} already existed — logged in")
    return r.json()["access_token"]


if __name__ == "__main__":
    raise SystemExit(main())
