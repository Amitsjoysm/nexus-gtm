-- nexus ledger TRAINING store, version 0001 (spec §18.3, §18.4).
-- Pseudonymised events and dataset examples in standard shapes. Rebuildable from the archive.
-- No names, addresses, phone numbers, links or company names: identities are HMAC keys and free text
-- has been through the scrubber (scrubber_version on every row). Idempotent.

CREATE TABLE IF NOT EXISTS nexus_ledger.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.events (
    event_id              text PRIMARY KEY,
    event_type            text NOT NULL,
    schema_version        integer NOT NULL,
    occurred_at           timestamptz NOT NULL,
    workspace_key         text NOT NULL,
    person_keys           text[] NOT NULL DEFAULT '{}',
    body                  jsonb NOT NULL,
    scrubber_version      integer NOT NULL,
    consent_terms_version text NOT NULL DEFAULT '',
    built_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.examples (
    example_id            text PRIMARY KEY,
    dataset               text NOT NULL,
    created_at            timestamptz NOT NULL,
    source_event_ids      text[] NOT NULL,
    workspace_key         text NOT NULL,
    person_keys           text[] NOT NULL DEFAULT '{}',
    split                 text NOT NULL CHECK (split IN ('train', 'val', 'test')),
    schema_version        integer NOT NULL,
    scrubber_version      integer NOT NULL,
    consent_terms_version text NOT NULL DEFAULT '',
    quality               jsonb NOT NULL DEFAULT '{}',
    record                jsonb NOT NULL,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- The builders' watermark: the last archive row they have turned into training and insights rows.
CREATE TABLE IF NOT EXISTS nexus_ledger.build_state (
    name        text PRIMARY KEY,
    archived_at timestamptz NOT NULL,
    event_id    text NOT NULL,
    built_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_workspace ON nexus_ledger.events (workspace_key);
CREATE INDEX IF NOT EXISTS events_person_keys ON nexus_ledger.events USING gin (person_keys);
CREATE INDEX IF NOT EXISTS examples_dataset_split ON nexus_ledger.examples (dataset, split);
CREATE INDEX IF NOT EXISTS examples_workspace ON nexus_ledger.examples (workspace_key);
CREATE INDEX IF NOT EXISTS examples_person_keys ON nexus_ledger.examples USING gin (person_keys);
