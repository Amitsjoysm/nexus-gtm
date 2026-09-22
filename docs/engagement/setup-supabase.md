# The three ledger stores (Supabase)

The training & insights ledger writes to **three separate Postgres databases** (spec §18.3, D27):

| Store | Holds | Who reads it |
|---|---|---|
| `archive` | every event as written, sealed | the rebuild jobs only |
| `training` | pseudonymised events and dataset tables | data scientists, through the export script |
| `insights` | identified engagement facts and profiles | the app, for the badges in §18.5 |

They are separate so a training extract can be handed to someone without also handing them the
archive, and so an insights outage cannot stop collection. This guide provisions them on Supabase.

Everything here is done **once, by the owner**. Claude never handles these credentials: the
connection strings are typed into the Control plane (Provider keys), where they are sealed, and they
appear in no API response, log line or audit row.

---

## 1. Three projects

Create three Supabase projects (or use the three already created for this):

| Store | Project ref |
|---|---|
| archive | `grnxfnagoxhdiqyscyfi` |
| training | `maqwelpushxsjdntkklz` |
| insights | `lrmzazesarmqtitpoyge` |

Any region is fine; put them all in the one closest to the app so shipping is not a transatlantic
round trip per batch.

## 2. A role per store, owning one schema and nothing else

In each project, open **SQL Editor** and run this, with your own password:

```sql
-- One role per store. It owns the ledger schema and has no rights anywhere else in the database.
create role nexus_ledger login password 'A-LONG-RANDOM-PASSWORD';
create schema if not exists nexus_ledger authorization nexus_ledger;

-- Nothing else in this database is reachable from this role.
revoke all on schema public from nexus_ledger;
revoke all on all tables in schema public from nexus_ledger;
alter role nexus_ledger set search_path = nexus_ledger;
```

Why a dedicated role rather than the project's `postgres` user:

- The app's own `check_store_dsn` **refuses** a superuser, a `BYPASSRLS` role, and the names
  `postgres` and `supabase_admin`. A connection string that can do anything is one leaked secret away
  from being able to do anything.
- The schema is `authorization nexus_ledger`, so the role **owns** it — that is what lets the Control
  plane apply the versioned schema, and it is what the Ledger tab reports as *owns nexus_ledger*.

Use a different password per project. Generate them with a password manager; they are typed once.

## 3. The connection string

Supabase gives two kinds of connection string. Use the **session pooler** one:

```
postgresql://nexus_ledger.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

- The username is `nexus_ledger.<project-ref>` — the pooler needs the project ref in the user name.
- Port **5432** is the session pooler; 6543 is the transaction pooler. Either works (the app sets
  `statement_cache_size=0` precisely because the pooler may run in transaction mode), but session
  mode is what the schema step needs.
- Direct connections (`db.<ref>.supabase.co`) are IPv6-only on new projects and will fail from an
  IPv4-only host.

## 4. Store the three strings and the pseudonymisation secret

In the app: **Control plane → Provider keys**, and add one key for each id:

| Provider key id | Value |
|---|---|
| `ledger_archive` | the archive project's connection string |
| `ledger_training` | the training project's connection string |
| `ledger_insights` | the insights project's connection string |
| `ledger_pseudonym` | a random secret, at least 32 characters, at least 16 distinct |

The pseudonymisation secret is what turns a person into a stable key and what seals the archive.
**Back it up with the database.** Losing it makes the archive unreadable and breaks every join
between stores — and, because erasure finds rows by that key, it breaks erasure too (spec §16).
Generate it with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Press **Test** on each key. The store keys probe by connecting and checking the role is not a
superuser; the secret is checked for length and variety.

## 5. Apply the schema

**Control plane → Ledger**. Each store shows *not configured*, *unreachable*, *N versions pending*
or *up to date*. Press **Apply schema** on each; it runs the versioned SQL in the repo
(`nexus/engagement/ledger/sql/<store>/`), records what it applied, and is safe to press twice.

Never run those files by hand in the SQL editor: the version table is what tells the shipper the
store is at a version the code understands.

## 6. Check it is collecting

Give it a few minutes with at least one consented workspace using the product, then:

- **Control plane → Platform health**: `ledger stores` is ok and `ledger outbox` says either nothing
  waiting or a small number with a recent oldest age.
- **Control plane → Ledger**: *Workspaces contributing* is not zero, and *Datasets last built* fills
  in within the hour.

A growing outbox with `retrying` above zero means a store is refusing writes — the detail line on the
store card says which and why.

## 7. Costs and retention

- The archive grows fastest: one row per event, sealed. On Supabase's free tier, watch the 500 MB
  database limit; a busy workspace is roughly 1–2 MB a day.
- The app's own `ledger_outbox` keeps rows for **7 days** after they are archived, then deletes them.
- Nothing in these stores is deleted by age. Deletion happens on request: a workspace switching
  training off, or a person erasure — both remove rows from all three stores and report what remains.
