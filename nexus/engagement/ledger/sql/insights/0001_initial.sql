-- nexus ledger INSIGHTS store, version 0001 (spec §18.3, §18.5, D25, D26).
-- Identified engagement facts and person/company profiles the app reads back. Identity is kept (D25)
-- so SDRs can be told about real prospects; the display rules in the app (D26) decide what a viewing
-- workspace may see. Rebuildable from the archive. Idempotent.

CREATE TABLE IF NOT EXISTS nexus_ledger.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.facts (
    event_id           text PRIMARY KEY,
    fact_type          text NOT NULL CHECK (fact_type IN ('send', 'reply', 'bounce', 'out_of_office')),
    person_email       text NOT NULL,
    person_key         text NOT NULL,
    company_domain     text NOT NULL DEFAULT '',
    workspace_key      text NOT NULL,
    occurred_at        timestamptz NOT NULL,
    local_hour         integer,
    local_weekday      integer,
    response_latency_s bigint,
    category           text NOT NULL DEFAULT '',
    attrs              jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS nexus_ledger.person_profiles (
    person_email       text PRIMARY KEY,
    person_key         text NOT NULL,
    company_domain     text NOT NULL DEFAULT '',
    best_weekday       integer,
    best_hour          integer,
    median_response_s  bigint,
    reply_propensity   real,
    last_reply_band    text NOT NULL DEFAULT '',
    ooo_periods        jsonb NOT NULL DEFAULT '[]',
    sends              integer NOT NULL DEFAULT 0,
    replies            integer NOT NULL DEFAULT 0,
    workspace_count    integer NOT NULL DEFAULT 0,
    workspace_keys     text[] NOT NULL DEFAULT '{}',
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.company_profiles (
    company_domain     text PRIMARY KEY,
    best_weekday       integer,
    best_hour          integer,
    median_response_s  bigint,
    reply_propensity   real,
    sends              integer NOT NULL DEFAULT 0,
    replies            integer NOT NULL DEFAULT 0,
    workspace_count    integer NOT NULL DEFAULT 0,
    workspace_keys     text[] NOT NULL DEFAULT '{}',
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS facts_person ON nexus_ledger.facts (person_email, occurred_at);
CREATE INDEX IF NOT EXISTS facts_person_key ON nexus_ledger.facts (person_key);
CREATE INDEX IF NOT EXISTS facts_company ON nexus_ledger.facts (company_domain);
CREATE INDEX IF NOT EXISTS facts_workspace ON nexus_ledger.facts (workspace_key);
CREATE INDEX IF NOT EXISTS person_profiles_key ON nexus_ledger.person_profiles (person_key);
