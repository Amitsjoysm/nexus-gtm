-- nexus ledger ARCHIVE store, version 0001 (spec §18.2, §18.3).
-- Every envelope as written, sealed. The source every other store is rebuilt from.
-- Applied by Control plane -> Ledger -> Apply schema, connected as the store's own role, which owns
-- the nexus_ledger schema (docs/engagement/setup-supabase.md). Idempotent.

CREATE TABLE IF NOT EXISTS nexus_ledger.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nexus_ledger.events (
    event_id       text PRIMARY KEY,
    event_type     text NOT NULL,
    schema_version integer NOT NULL,
    occurred_at    timestamptz NOT NULL,
    tenant_id      text NOT NULL,
    -- Fernet-sealed JSON envelope. Clear columns above exist only to index, ship and delete by.
    sealed         text NOT NULL,
    -- HMAC person keys of everyone the event is about, so an erasure can find the rows.
    person_keys    text[] NOT NULL DEFAULT '{}',
    archived_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_archived_at ON nexus_ledger.events (archived_at, event_id);
CREATE INDEX IF NOT EXISTS events_tenant ON nexus_ledger.events (tenant_id);
CREATE INDEX IF NOT EXISTS events_person_keys ON nexus_ledger.events USING gin (person_keys);
