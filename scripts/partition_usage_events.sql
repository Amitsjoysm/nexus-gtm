-- Convert billing_usage_events to monthly range partitions.
--
-- NOT an Alembic migration, deliberately. Postgres cannot ALTER an existing table into a
-- partitioned one: it requires creating the partitioned table, copying every row, and swapping
-- under a lock. That is a maintenance window on the table recording what customers are billed for,
-- and this project's migrations are additive-only and must replay onto an empty database.
--
-- Run this during a planned window, with the app stopped or in read-only mode. The continuous,
-- always-safe half of retention lives in nexus/billing/retention.py and needs no window.
--
-- Sizing first: below roughly 10M rows the composite indexes on the quota path are sufficient and
-- partitioning adds operational cost for no measurable gain. Check before scheduling anything:
--
--   SELECT count(*) FROM billing_usage_events;
--   EXPLAIN ANALYZE SELECT sum(quantity) FROM billing_usage_events
--     WHERE tenant_id = $1 AND capability_id = $2 AND rolled_at IS NULL;

BEGIN;

-- 1. The partitioned replacement, keyed on the column every hot-path query filters by.
CREATE TABLE billing_usage_events_part (LIKE billing_usage_events INCLUDING ALL)
    PARTITION BY RANGE (created_at);

-- 2. Partitions covering existing data plus a runway. `nexus/billing/retention.py` is the
--    always-safe pruning half; creating future partitions ahead of time is what keeps inserts from
--    failing the moment the clock crosses a month boundary with no partition to land in.
--    Generate these for the range you actually need:
--
--      CREATE TABLE billing_usage_events_2026_08 PARTITION OF billing_usage_events_part
--        FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');

-- 3. Copy, swap, verify. Do NOT drop the original until counts match.
--    INSERT INTO billing_usage_events_part SELECT * FROM billing_usage_events;
--    SELECT count(*) FROM billing_usage_events;        -- must equal the next line
--    SELECT count(*) FROM billing_usage_events_part;
--    ALTER TABLE billing_usage_events RENAME TO billing_usage_events_old;
--    ALTER TABLE billing_usage_events_part RENAME TO billing_usage_events;

-- 4. RLS is NOT inherited by the rename. Re-run the enrolment or every tenant reads every other
--    tenant's usage — the failure mode is silent, because RLS misses return rows rather than errors.
--    python scripts/apply_rls.py

COMMIT;

-- Keep billing_usage_events_old until at least one full billing cycle has closed cleanly against
-- the partitioned table. It is the only rollback that does not involve a backup restore.
