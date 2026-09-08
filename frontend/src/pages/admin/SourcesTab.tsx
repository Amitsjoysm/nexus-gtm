import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  Badge,
  Button,
  CardHeader,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
} from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import { formatNumber, humanize } from "@/lib/format";
import type {
  SourceDatabase,
  SourceDryRun,
  SourceMapping,
  SourceTable,
  SourceVocabulary,
} from "@/lib/types";
import styles from "./SourcesTab.module.css";

/**
 * External source databases: a read-only DSN into somebody else's Postgres, read AHEAD of the paid
 * enrichment APIs so an answer is bought once for every tenant.
 *
 * The whole subsystem shipped with a complete API and **no screen**, so registering a source meant
 * a superadmin issuing seven curl requests in the right order. `source_databases` had zero rows.
 *
 * **The ladder is the screen.** `registered → connected → introspected → mapped → verified`, and
 * each rung's action is offered only when the source has reached the one before it. That is not
 * decoration: only the server advances `status`, re-introspecting or re-mapping CLEARS the proof,
 * and a source that stayed `verified` after its table was rebuilt is the wrong-attribution bug this
 * codebase has already shipped six times.
 *
 * Two things this screen deliberately cannot do:
 *
 * * **Show the connection string.** No response carries it in any form, and an affordance for it
 *   would turn every read of this console into a credential disclosure.
 * * **Run SQL.** The mapping form sends table and column NAMES, checked against what introspection
 *   actually discovered. A console that ran free SQL against a customer's production database is a
 *   blast radius that adds nothing over a psql session.
 */

const RUNGS = ["registered", "connected", "introspected", "mapped", "verified"] as const;

function rungIndex(status: string): number {
  const i = RUNGS.indexOf(status as (typeof RUNGS)[number]);
  return i < 0 ? 0 : i;
}

function statusChip(s: SourceDatabase): {
  tone: "success" | "warning" | "danger" | "neutral" | "accent";
  text: string;
} {
  if (s.status === "failed") return { tone: "danger", text: "Failed" };
  if (s.usable) return { tone: "success", text: "Live" };
  if (s.status === "verified") return { tone: "accent", text: "Verified, switched off" };
  if (s.status === "registered") return { tone: "neutral", text: "Registered" };
  return { tone: "warning", text: humanize(s.status) };
}

export function SourcesTab() {
  const api = useApiClient();
  const sources = useApi<SourceDatabase[]>((signal) => api.adminSourceDatabases(signal), []);
  const vocab = useApi<SourceVocabulary>((signal) => api.sourceDatabaseVocabulary(signal), []);
  const [adding, setAdding] = useState(false);

  return (
    <div className={styles.wrap}>
      <CardHeader
        title="External source databases"
        subtitle="A read-only connection to a warehouse you already pay for, read ahead of the paid enrichment APIs. Answers land in the shared company and people stores, so one lookup serves every workspace."
        actions={
          <Button size="sm" iconLeft={<Icons.PlusIcon />} onClick={() => setAdding(true)}>
            Register a database
          </Button>
        }
      />

      <p className={styles.warning}>
        A mis-mapped source is wrong for <strong>every</strong> workspace at once, which is why
        sources are platform-wide and why nothing is read from one until a dry run proves the
        mapping on real rows.
      </p>

      <DataState
        state={sources}
        errorTitle="Couldn't load source databases"
        skeleton={<Skeleton width="100%" height={200} />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <div className={styles.empty}>
            <h3 className={styles.emptyTitle}>No source databases yet</h3>
            <p className={styles.emptyBody}>
              Register one and the enrichment waterfall tries it before Exa, Apify or any other
              billed provider. A hit is still metered to the customer: what this saves is our cost,
              not their price.
            </p>
            <Button iconLeft={<Icons.PlusIcon />} onClick={() => setAdding(true)}>
              Register a database
            </Button>
          </div>
        }
      >
        {(rows) => (
          <ul className={styles.list}>
            {rows.map((row) => (
              <SourceRow
                key={row.id}
                source={row}
                vocabulary={vocab.data ?? null}
                onChanged={() => sources.refetch()}
              />
            ))}
          </ul>
        )}
      </DataState>

      {adding && (
        <RegisterDialog
          onClose={() => setAdding(false)}
          onRegistered={() => {
            setAdding(false);
            sources.refetch();
          }}
        />
      )}
    </div>
  );
}

/* ---- one source ------------------------------------------------------------------------------ */

function SourceRow({
  source,
  vocabulary,
  onChanged,
}: {
  source: SourceDatabase;
  vocabulary: SourceVocabulary | null;
  onChanged: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [mapping, setMapping] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const chip = statusChip(source);
  const rung = rungIndex(source.status);
  const tables = source.discovered_schema?.tables ?? [];
  const dryRun = (source.dry_run as SourceDryRun).entity ? (source.dry_run as SourceDryRun) : null;
  const mapped = (source.mapping as SourceMapping).entity ? (source.mapping as SourceMapping) : null;

  async function run(label: string, fn: () => Promise<unknown>, ok: (r: never) => string) {
    setBusy(label);
    try {
      const result = await fn();
      onChanged();
      toast.success(ok(result as never));
    } catch (err) {
      toast.error(
        `${label} failed`,
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className={styles.row}>
      <div className={styles.head}>
        <div className={styles.headText}>
          <span className={styles.name}>{source.name}</span>
          {/* Host and database. The credentials are never in any response. */}
          <span className={styles.dsn}>{source.dsn_redacted}</span>
        </div>
        <Badge tone={chip.tone} dot>
          {chip.text}
        </Badge>
      </div>

      <Ladder status={source.status} />

      {source.last_error && (
        <p className={styles.error} role="status">
          {source.last_error}
        </p>
      )}

      {mapped && (
        <p className={styles.detail}>
          Mapped as <strong>{mapped.entity}</strong> from{" "}
          <code className={styles.code}>
            {mapped.schema}.{mapped.table}
          </code>{" "}
          — {Object.entries(mapped.columns).map(([f, c]) => `${f} ← ${c}`).join(", ")}
        </p>
      )}

      {dryRun && <DryRunReport report={dryRun} />}

      <div className={styles.actions}>
        <Button
          size="sm"
          variant="ghost"
          loading={busy === "Connection test"}
          onClick={() =>
            run("Connection test", () => api.testSourceDatabase(source.id), (r: never) => {
              const t = r as unknown as { database: string; read_only: boolean };
              return `Reached ${t.database}${t.read_only ? " (read-only)" : ""}`;
            })
          }
        >
          Test connection
        </Button>

        {/* Introspection needs a proven connection, and re-running it clears the proof —
            deliberately, because a mapping verified against a schema that has since been rebuilt is
            worse than no mapping at all. */}
        <Button
          size="sm"
          variant="ghost"
          disabled={rung < 1}
          loading={busy === "Introspection"}
          onClick={() =>
            run("Introspection", () => api.introspectSourceDatabase(source.id), () =>
              "Schema read",
            )
          }
        >
          {tables.length ? "Re-read schema" : "Read schema"}
        </Button>

        <Button
          size="sm"
          variant="ghost"
          disabled={tables.length === 0}
          onClick={() => setMapping(true)}
        >
          {mapped ? "Change mapping" : "Map columns"}
        </Button>

        <Button
          size="sm"
          variant="ghost"
          disabled={!mapped}
          loading={busy === "Dry run"}
          onClick={() =>
            run("Dry run", () => api.dryRunSourceDatabase(source.id), () =>
              "Dry run finished — read the result below",
            )
          }
        >
          Dry run
        </Button>

        <div className={styles.spacer} />

        {/* Enabling is refused below `verified`: that is what makes the dry run a gate rather than
            a suggestion. Disabling is never refused. */}
        <Button
          size="sm"
          variant={source.enabled ? "ghost" : "secondary"}
          disabled={!source.enabled && source.status !== "verified"}
          loading={busy === "Switch"}
          onClick={() =>
            run("Switch", () => api.setSourceDatabaseEnabled(source.id, !source.enabled), () =>
              source.enabled ? "Source switched off" : "Source is live",
            )
          }
        >
          {source.enabled ? "Switch off" : "Switch on"}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label={`Delete ${source.name}`}
          onClick={() => setConfirmDelete(true)}
        >
          <Icons.TrashIcon />
        </Button>
      </div>

      {mapping && (
        <MappingDialog
          source={source}
          vocabulary={vocabulary}
          onClose={() => setMapping(false)}
          onSaved={() => {
            setMapping(false);
            onChanged();
          }}
        />
      )}

      <Modal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={`Delete ${source.name}?`}
        description="The stored connection string is destroyed. Enrichment falls back to the paid providers, which costs money rather than losing data."
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Keep it
            </Button>
            <Button
              variant="danger"
              onClick={async () => {
                try {
                  await api.deleteSourceDatabase(source.id);
                  setConfirmDelete(false);
                  onChanged();
                  toast.success("Source deleted");
                } catch (err) {
                  toast.error(
                    "Couldn't delete",
                    err instanceof ApiError ? err.detail : "Please try again.",
                  );
                }
              }}
            >
              Delete source
            </Button>
          </>
        }
      >
        <p>You will need the connection string again to re-register it.</p>
      </Modal>
    </li>
  );
}

/** The ladder, rendered as the ladder. Each rung is a fact the server established, and the reason
 *  the screen shows all five is that "mapped" and "verified" look identical in a status badge and
 *  mean the difference between a source that is proven and one that is merely configured. */
function Ladder({ status }: { status: string }) {
  const reached = rungIndex(status);
  const failed = status === "failed";
  return (
    <ol className={styles.ladder} aria-label="Source readiness">
      {RUNGS.map((rung, i) => (
        <li
          key={rung}
          className={styles.rung}
          data-state={failed ? "failed" : i <= reached ? "done" : "todo"}
        >
          <span className={styles.rungDot} aria-hidden="true" />
          <span className={styles.rungLabel}>{humanize(rung)}</span>
        </li>
      ))}
    </ol>
  );
}

/** `usable_rows` is the number that matters, not `rows`. A mapping returning 25 rows of which 2
 *  carry an identity is pointed at the wrong column, and it looks like a complete success if you
 *  only count rows. */
function DryRunReport({ report }: { report: SourceDryRun }) {
  return (
    <div className={styles.dryRun} data-verified={report.verified}>
      <div className={styles.dryRunHead}>
        <Badge tone={report.verified ? "success" : "warning"} dot>
          {report.verified ? "Mapping proven" : "Mapping not proven"}
        </Badge>
        <span className={styles.detail}>
          {formatNumber(report.usable_rows)} of {formatNumber(report.rows)} sampled rows carried an
          identity ({report.identity_field}).
        </span>
      </div>
      {!report.verified && report.rows > 0 && (
        <p className={styles.detail}>
          The source is reachable and the query ran, so this is not a connection problem — the
          mapping is on the wrong column.
        </p>
      )}
      <details className={styles.statement}>
        <summary>What ran, and what came back</summary>
        <pre className={styles.pre}>{report.statement}</pre>
        <pre className={styles.pre}>{JSON.stringify(report.sample, null, 2)}</pre>
      </details>
    </div>
  );
}

/* ---- register --------------------------------------------------------------------------------- */

function RegisterDialog({
  onClose,
  onRegistered,
}: {
  onClose: () => void;
  onRegistered: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [dsn, setDsn] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.registerSourceDatabase({ name: name.trim(), dsn: dsn.trim(), kind: "postgres" });
      toast.success("Source registered", "Test the connection, then read its schema.");
      onRegistered();
    } catch (err) {
      toast.error(
        "Couldn't register",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Register a source database"
      description="Read-only, platform-wide, and never read from until a dry run proves the mapping."
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!name.trim() || !dsn.trim()}>
            Register source
          </Button>
        </>
      }
    >
      <form className={styles.form} onSubmit={submit} noValidate>
        <Field label="Name" required hint="How this source appears in the console.">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Warehouse — company firmographics"
            autoFocus
          />
        </Field>
        <Field
          label="Connection string"
          required
          hint="A read-only Postgres role. Stored encrypted and never shown again, here or anywhere else."
        >
          <Input
            type="password"
            autoComplete="off"
            value={dsn}
            onChange={(e) => setDsn(e.target.value)}
            placeholder="postgresql://readonly:…@warehouse.example.com:5432/analytics"
          />
        </Field>
        <p className={styles.formNote}>
          Private and loopback addresses are refused. A form that accepts any address and reports
          whether it connected is a port scanner, and pointed at the container network it is a read
          oracle.
        </p>
      </form>
    </Modal>
  );
}

/* ---- mapping ---------------------------------------------------------------------------------- */

function MappingDialog({
  source,
  vocabulary,
  onClose,
  onSaved,
}: {
  source: SourceDatabase;
  vocabulary: SourceVocabulary | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const tables: SourceTable[] = source.discovered_schema?.tables ?? [];
  const existing = (source.mapping as SourceMapping).entity
    ? (source.mapping as SourceMapping)
    : null;

  const entities = Object.keys(vocabulary?.entities ?? { company: [], person: [] });
  const [entity, setEntity] = useState(existing?.entity ?? entities[0] ?? "company");
  const [tableKey, setTableKey] = useState(
    existing ? `${existing.schema}.${existing.table}` : tables[0] ? `${tables[0].schema}.${tables[0].table}` : "",
  );
  const [columns, setColumns] = useState<Record<string, string>>(existing?.columns ?? {});
  const [busy, setBusy] = useState(false);

  const table = useMemo(
    () => tables.find((t) => `${t.schema}.${t.table}` === tableKey) ?? null,
    [tables, tableKey],
  );
  const appFields = vocabulary?.entities[entity] ?? [];
  const required = vocabulary?.required[entity] ?? [];

  // The `person` rule is "either linkedin_url or email", which is not expressible as a required
  // list — so it is checked here the way the server checks it, rather than approximated.
  const identityMissing =
    entity === "person"
      ? !columns.linkedin_url && !columns.email
      : required.some((f) => !columns[f]);

  async function save() {
    if (!table) return;
    setBusy(true);
    try {
      const clean = Object.fromEntries(
        Object.entries(columns).filter(([, v]) => v),
      ) as Record<string, string>;
      await api.setSourceDatabaseMapping(source.id, {
        entity,
        schema_name: table.schema,
        table: table.table,
        columns: clean,
      });
      toast.success("Mapping saved", "Run a dry run to prove it on real rows.");
      onSaved();
    } catch (err) {
      toast.error("Couldn't save mapping", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Map ${source.name}`}
      description="Match this source's columns onto app fields. Saving replaces any existing proof, so a dry run is needed again."
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!table || identityMissing}>
            Save mapping
          </Button>
        </>
      }
    >
      <div className={styles.form}>
        <div className={styles.formRow}>
          <Field label="Maps onto">
            <Select
              value={entity}
              onChange={(e) => {
                setEntity(e.target.value);
                setColumns({});
              }}
              options={entities.map((v) => ({ value: v, label: humanize(v) }))}
            />
          </Field>
          <Field label="Source table">
            <Select
              value={tableKey}
              onChange={(e) => {
                setTableKey(e.target.value);
                setColumns({});
              }}
              options={tables.map((t) => ({
                value: `${t.schema}.${t.table}`,
                label: `${t.schema}.${t.table} (${t.columns.length} columns)`,
              }))}
            />
          </Field>
        </div>

        {vocabulary?.identity_note[entity] && (
          <p className={styles.formNote}>{vocabulary.identity_note[entity]}</p>
        )}

        {table && (
          <div className={styles.mapGrid}>
            {appFields.map((field) => (
              <Field
                key={field}
                label={humanize(field)}
                required={
                  entity === "person" ? field === "linkedin_url" || field === "email" : required.includes(field)
                }
              >
                <Select
                  value={columns[field] ?? ""}
                  placeholder="Not mapped"
                  onChange={(e) =>
                    setColumns((c) => ({ ...c, [field]: e.target.value }))
                  }
                  options={table.columns.map((c) => ({
                    value: c.name,
                    label: `${c.name} · ${c.type}`,
                  }))}
                />
              </Field>
            ))}
          </div>
        )}

        {identityMissing && (
          <p className={styles.warn} role="alert">
            {entity === "person"
              ? "Map a LinkedIn URL or an email. A name is not an identity, and getting a person wrong means a rep phones a stranger with somebody else's context."
              : `Map ${required.join(", ")}. A company is identified by its domain and nothing else.`}
          </p>
        )}
      </div>
    </Modal>
  );
}

export default SourcesTab;
