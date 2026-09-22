"""Archive → training and insights, incrementally (spec §18.2, §18.4, §18.5).

Reads the archive in the order rows were written, from the watermark in
``nexus_ledger.build_state`` (kept in the training store, because that is where the examples are),
and writes both derived stores from the same batch. Everything it writes is an upsert keyed on an id
derived from the event, so re-running a batch — after a crash, or deliberately to replay a scrubber
fix — converges instead of duplicating.

The watermark advances only after both stores have taken the batch. Re-running therefore costs a
repeated upsert, never a gap; a gap is the one failure that would be invisible, because a missing
example looks exactly like a workspace that did not send that day.

Profiles are RECOMPUTED from the facts that remain for each person touched, never incremented. An
erasure or a workspace deletion removes facts, and a counter would keep describing data we no longer
hold.
"""
from __future__ import annotations

import json
import logging

from nexus.engagement.ledger import datasets, insights
from nexus.engagement.ledger.shipper import as_datetime
from nexus.engagement.ledger.stores import connect

logger = logging.getLogger("nexus.engagement.ledger.builder")

WATERMARK = "datasets"
BATCH = 500
MAX_BATCHES = 10


async def build_once(limit: int = BATCH) -> dict:
    """One batch. Returns what it wrote, or what stopped it."""
    from nexus.engagement import config

    secret = await config.pseudonym_secret()
    if not secret:
        return {"built": 0, "skipped": "the pseudonymisation secret is not configured"}

    async with connect("training") as training:
        mark = await training.fetchrow(
            "SELECT archived_at, event_id FROM nexus_ledger.build_state WHERE name = $1", WATERMARK)
    since_at = mark["archived_at"] if mark else None
    since_id = mark["event_id"] if mark else ""

    async with connect("archive") as archive:
        if since_at is None:
            rows = await archive.fetch(
                "SELECT event_id, archived_at, sealed FROM nexus_ledger.events "
                "ORDER BY archived_at, event_id LIMIT $1", limit)
        else:
            rows = await archive.fetch(
                "SELECT event_id, archived_at, sealed FROM nexus_ledger.events "
                "WHERE (archived_at, event_id) > ($1, $2) ORDER BY archived_at, event_id LIMIT $3",
                since_at, since_id, limit)
    if not rows:
        return {"built": 0}

    from nexus.engagement.ledger.shipper import open_document

    events, examples, patches, facts, categories = [], [], [], [], []
    for row in rows:
        try:
            document = open_document(secret, row["sealed"])
        except Exception:
            # An unreadable row is almost always a rotated secret. Skipping keeps the rest of the
            # batch moving; the watermark still advances, and the archive row is the evidence.
            logger.warning("ledger archive row %s could not be opened", row["event_id"])
            continue
        built = datasets.build(document, secret)
        if built.event:
            events.append(built.event)
        examples.extend(built.examples)
        patches.extend(built.patches)
        facts.extend(built.facts)
        categories.extend(built.fact_categories)

    await _write_training(events, examples, patches)
    touched = await _write_insights(facts, categories)
    await _advance(rows[-1]["archived_at"], rows[-1]["event_id"])
    return {"built": len(rows), "examples": len(examples), "patches": len(patches),
            "facts": len(facts), "profiles": touched}


async def build(max_batches: int = MAX_BATCHES) -> dict:
    total = {"built": 0, "examples": 0, "facts": 0, "profiles": 0}
    for _ in range(max_batches):
        result = await build_once()
        if result.get("skipped"):
            return result
        for key in total:
            total[key] += result.get(key, 0)
        if result["built"] < BATCH:
            break
    return total


async def _advance(archived_at, event_id: str) -> None:
    async with connect("training") as training:
        await training.execute(
            "INSERT INTO nexus_ledger.build_state (name, archived_at, event_id, built_at) "
            "VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (name) DO UPDATE SET archived_at = EXCLUDED.archived_at, "
            "event_id = EXCLUDED.event_id, built_at = now()",
            WATERMARK, archived_at, event_id)


async def last_built_at():
    async with connect("training") as training:
        return await training.fetchval(
            "SELECT built_at FROM nexus_ledger.build_state WHERE name = $1", WATERMARK)


async def _write_training(events: list[dict], examples: list, patches: list) -> None:
    if not (events or examples or patches):
        return
    async with connect("training") as training:
        async with training.transaction():
            if events:
                await training.executemany(
                    "INSERT INTO nexus_ledger.events (event_id, event_type, schema_version, "
                    " occurred_at, workspace_key, person_keys, body, scrubber_version, "
                    " consent_terms_version) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9) "
                    "ON CONFLICT (event_id) DO UPDATE SET body = EXCLUDED.body, "
                    "scrubber_version = EXCLUDED.scrubber_version",
                    [(e["event_id"], e["event_type"], e["schema_version"],
                      as_datetime(e["occurred_at"]),
                      e["workspace_key"], e["person_keys"], json.dumps(e["body"]),
                      e["scrubber_version"], e["consent_terms_version"]) for e in events])
            if examples:
                await training.executemany(
                    "INSERT INTO nexus_ledger.examples (example_id, dataset, created_at, "
                    " source_event_ids, workspace_key, person_keys, split, schema_version, "
                    " scrubber_version, consent_terms_version, quality, record) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, "
                    " $12::jsonb) "
                    "ON CONFLICT (example_id) DO UPDATE SET "
                    # The fresh record, with any labels or corrections already learned put BACK over
                    # it, so replaying the archive (for a scrubber fix) cannot un-learn a label a
                    # later event set. `jsonb_strip_nulls` drops the keys the stored row never had.
                    "  record = EXCLUDED.record || jsonb_strip_nulls(jsonb_build_object("
                    "    'labels', nexus_ledger.examples.record->'labels', "
                    "    'output', nexus_ledger.examples.record->'output')), "
                    "  quality = EXCLUDED.quality, scrubber_version = EXCLUDED.scrubber_version, "
                    "  source_event_ids = ARRAY(SELECT DISTINCT unnest("
                    "     nexus_ledger.examples.source_event_ids || EXCLUDED.source_event_ids)), "
                    "  updated_at = now()",
                    [(x.example_id, x.dataset, as_datetime(x.created_at), x.source_event_ids,
                      x.workspace_key,
                      x.person_keys, datasets.split_of(x.workspace_key), datasets.SCHEMA_VERSION,
                      datasets.SCRUBBER_VERSION, x.consent_terms_version, json.dumps(x.quality),
                      json.dumps(x.record)) for x in examples])
            for patch in patches:
                await training.execute(
                    "UPDATE nexus_ledger.examples SET record = jsonb_set(record, $2::text[], "
                    "  coalesce(record #> $2::text[], '{}'::jsonb) || $3::jsonb, true), "
                    "  source_event_ids = ARRAY(SELECT DISTINCT unnest(source_event_ids || $4)), "
                    "  updated_at = now() "
                    "WHERE example_id = $1",
                    patch.example_id, [patch.path], json.dumps(patch.values),
                    [patch.source_event_id])


async def _write_insights(facts: list, categories: list) -> int:
    """Facts, late-arriving categories, then a recomputed profile for everyone touched."""
    if not (facts or categories):
        return 0
    async with connect("insights") as store:
        async with store.transaction():
            if facts:
                await store.executemany(
                    "INSERT INTO nexus_ledger.facts (event_id, fact_type, person_email, person_key,"
                    " company_domain, workspace_key, occurred_at, local_hour, local_weekday, "
                    " response_latency_s, category, attrs) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb) "
                    "ON CONFLICT (event_id) DO NOTHING",
                    [(f.event_id, f.fact_type, f.person_email, f.person_key, f.company_domain,
                      f.workspace_key, as_datetime(f.occurred_at), f.local_hour,
                      f.local_weekday,
                      f.response_latency_s, f.category, json.dumps(f.attrs)) for f in facts])
            for category in categories:
                await store.execute(
                    "UPDATE nexus_ledger.facts SET category = $2 "
                    "WHERE fact_type = 'reply' AND attrs->>'message_id' = $1",
                    category.message_id, category.category)
        people = sorted({f.person_email for f in facts if f.person_email})
        domains = sorted({f.company_domain for f in facts if f.company_domain})
        for email in people:
            await refresh_person(store, email)
        for domain in domains:
            await refresh_company(store, domain)
    return len(people)


async def refresh_person(store, person_email: str) -> None:
    rows = await store.fetch(
        "SELECT fact_type, person_key, company_domain, workspace_key, occurred_at, local_hour, "
        "       local_weekday, response_latency_s, attrs "
        "FROM nexus_ledger.facts WHERE person_email = $1", person_email)
    profile = insights.person_profile(person_email, [dict(row) for row in rows])
    if profile is None:
        await store.execute("DELETE FROM nexus_ledger.person_profiles WHERE person_email = $1",
                            person_email)
        return
    await store.execute(
        "INSERT INTO nexus_ledger.person_profiles (person_email, person_key, company_domain, "
        " best_weekday, best_hour, median_response_s, reply_propensity, last_reply_band, "
        " ooo_periods, sends, replies, workspace_count, workspace_keys, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, now()) "
        "ON CONFLICT (person_email) DO UPDATE SET person_key = EXCLUDED.person_key, "
        " company_domain = EXCLUDED.company_domain, best_weekday = EXCLUDED.best_weekday, "
        " best_hour = EXCLUDED.best_hour, median_response_s = EXCLUDED.median_response_s, "
        " reply_propensity = EXCLUDED.reply_propensity, last_reply_band = EXCLUDED.last_reply_band, "
        " ooo_periods = EXCLUDED.ooo_periods, sends = EXCLUDED.sends, replies = EXCLUDED.replies, "
        " workspace_count = EXCLUDED.workspace_count, workspace_keys = EXCLUDED.workspace_keys, "
        " updated_at = now()",
        profile["person_email"], profile["person_key"], profile["company_domain"],
        profile["best_weekday"], profile["best_hour"], profile["median_response_s"],
        profile["reply_propensity"], profile["last_reply_band"], json.dumps(profile["ooo_periods"]),
        profile["sends"], profile["replies"], profile["workspace_count"],
        profile["workspace_keys"])


async def refresh_company(store, company_domain: str) -> None:
    rows = await store.fetch(
        "SELECT fact_type, person_key, company_domain, workspace_key, occurred_at, local_hour, "
        "       local_weekday, response_latency_s, attrs "
        "FROM nexus_ledger.facts WHERE company_domain = $1", company_domain)
    profile = insights.company_profile(company_domain, [dict(row) for row in rows])
    if profile is None:
        await store.execute("DELETE FROM nexus_ledger.company_profiles WHERE company_domain = $1",
                            company_domain)
        return
    await store.execute(
        "INSERT INTO nexus_ledger.company_profiles (company_domain, best_weekday, best_hour, "
        " median_response_s, reply_propensity, sends, replies, workspace_count, workspace_keys, "
        " updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now()) "
        "ON CONFLICT (company_domain) DO UPDATE SET best_weekday = EXCLUDED.best_weekday, "
        " best_hour = EXCLUDED.best_hour, median_response_s = EXCLUDED.median_response_s, "
        " reply_propensity = EXCLUDED.reply_propensity, sends = EXCLUDED.sends, "
        " replies = EXCLUDED.replies, workspace_count = EXCLUDED.workspace_count, "
        " workspace_keys = EXCLUDED.workspace_keys, updated_at = now()",
        profile["company_domain"], profile["best_weekday"], profile["best_hour"],
        profile["median_response_s"], profile["reply_propensity"], profile["sends"],
        profile["replies"], profile["workspace_count"], profile["workspace_keys"])
