import { useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Button,
  Field,
  Icons,
  Modal,
  Select,
  Spinner,
} from "@/components/ui";
import type { SelectOption } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { parseCsv } from "@/lib/csv";
import type { ParsedCsv } from "@/lib/csv";
import type {
  CsvImportResult,
  CustomFieldDef,
  CustomFieldEntity,
} from "@/lib/types";
import styles from "./ImportCsvModal.module.css";

const MAX_BYTES = 5 * 1024 * 1024; // 5 MB — preview parses client-side, server takes the raw file.
const PREVIEW_ROWS = 8;
const IGNORE = "__ignore__";
const NEW = "__new__";

/** Mirror the server's key normalisation so "Import as new column" previews the real key. */
function slug(label: string): string {
  const base = label.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  return base.slice(0, 60) || "field";
}

function guessMatchColumn(headers: string[], entity: CustomFieldEntity): string {
  const want = entity === "contact" ? /(e-?mail)/i : /(domain|website|url)/i;
  return headers.find((h) => want.test(h)) ?? headers[0] ?? "";
}

export interface ImportCsvModalProps {
  open: boolean;
  /** Which side of the CRM the values attach to; decides the match key (domain vs. email). */
  entity: CustomFieldEntity;
  onClose: () => void;
  /** Fired after a successful import so the results table can refetch new columns. */
  onImported: (result: CsvImportResult) => void;
}

/**
 * Three-step proprietary-data import: drop a CSV, map its columns onto custom fields (matching
 * accounts by domain / contacts by email), then import. Parsing for the preview is client-side
 * and bounded; the raw `File` streams to `POST /custom-fields/import` for the real upsert.
 */
export function ImportCsvModal({ open, entity, onClose, onImported }: ImportCsvModalProps) {
  const api = useApiClient();
  // Only an open modal should hit the admin-gated catalog endpoint.
  const existing = useApi<CustomFieldDef[]>(
    (signal) => (open ? api.listCustomFields(entity, signal) : Promise.resolve([])),
    [entity, open],
  );

  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [parsed, setParsed] = useState<ParsedCsv | null>(null);
  const [matchColumn, setMatchColumn] = useState("");
  // csvColumn -> IGNORE | NEW | existing field key
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [fileError, setFileError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [result, setResult] = useState<CsvImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);

  // Wipe state on close so reopening starts clean.
  useEffect(() => {
    if (open) return;
    setFile(null);
    setParsed(null);
    setMatchColumn("");
    setMapping({});
    setFileError(null);
    setServerError(null);
    setResult(null);
    setBusy(false);
    setDragOver(false);
  }, [open]);

  const matchLabel = entity === "contact" ? "email" : "domain";

  async function ingest(f: File) {
    setFileError(null);
    setServerError(null);
    setResult(null);
    if (!/\.csv$/i.test(f.name) && f.type !== "text/csv") {
      setFileError("Choose a .csv file.");
      return;
    }
    if (f.size > MAX_BYTES) {
      setFileError("That file is larger than 5 MB. Split it into smaller files.");
      return;
    }
    let text: string;
    try {
      text = await f.text();
    } catch {
      setFileError("Couldn't read that file. Try again.");
      return;
    }
    const p = parseCsv(text, { maxRows: PREVIEW_ROWS });
    if (p.headers.length === 0) {
      setFileError("No columns found. Is the first row a header?");
      return;
    }
    const match = guessMatchColumn(p.headers, entity);
    const init: Record<string, string> = {};
    for (const h of p.headers) {
      if (h === match) continue;
      const hit = existing.data?.find((d) => d.key === slug(h));
      init[h] = hit ? hit.key : NEW;
    }
    setFile(f);
    setParsed(p);
    setMatchColumn(match);
    setMapping(init);
  }

  function onFileInput(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) void ingest(f);
    e.target.value = ""; // allow re-selecting the same file
  }

  function changeMatch(next: string) {
    setMatchColumn(next);
    setMapping((prev) => {
      const out = { ...prev };
      delete out[next];
      if (parsed) {
        for (const h of parsed.headers) {
          if (h !== next && !(h in out)) out[h] = NEW;
        }
      }
      return out;
    });
  }

  function reset() {
    setFile(null);
    setParsed(null);
    setMatchColumn("");
    setMapping({});
    setFileError(null);
    setServerError(null);
  }

  const fieldOptions = useMemo<SelectOption[]>(() => {
    const opts: SelectOption[] = [
      { value: IGNORE, label: "Ignore this column" },
      { value: NEW, label: "Import as new column" },
    ];
    for (const d of existing.data ?? []) opts.push({ value: d.key, label: d.label });
    return opts;
  }, [existing.data]);

  const headerOptions = useMemo<SelectOption[]>(
    () => (parsed?.headers ?? []).map((h) => ({ value: h, label: h })),
    [parsed],
  );

  const mappedCount = useMemo(
    () =>
      Object.entries(mapping).filter(([col, choice]) => col !== matchColumn && choice !== IGNORE)
        .length,
    [mapping, matchColumn],
  );

  function buildMapping(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [col, choice] of Object.entries(mapping)) {
      if (col === matchColumn || choice === IGNORE) continue;
      out[col] = choice === NEW ? slug(col) : choice;
    }
    return out;
  }

  async function submit() {
    if (!file || matchColumn === "" || mappedCount === 0) return;
    setBusy(true);
    setServerError(null);
    try {
      const res = await api.importCustomFieldsCsv({
        entity,
        matchColumn,
        mapping: buildMapping(),
        file,
      });
      setResult(res);
    } catch (err) {
      setServerError(err instanceof ApiError ? err.detail : "Import failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  function finish() {
    if (result) onImported(result);
    onClose();
  }

  const phase: "drop" | "map" | "done" = result ? "done" : parsed ? "map" : "drop";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Import proprietary data"
      description={`Match rows by ${matchLabel} and attach their columns as custom fields.`}
      size="lg"
      footer={
        phase === "done" ? (
          <Button onClick={finish}>Done</Button>
        ) : phase === "map" ? (
          <>
            <Button variant="ghost" onClick={reset} disabled={busy}>
              Choose another file
            </Button>
            <Button variant="secondary" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={submit} loading={busy} disabled={mappedCount === 0 || matchColumn === ""}>
              {mappedCount === 0
                ? "Map a column"
                : `Import ${mappedCount} column${mappedCount === 1 ? "" : "s"}`}
            </Button>
          </>
        ) : (
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
        )
      }
    >
      {phase === "drop" && (
        <div className={styles.dropStep}>
          <div
            className={cn(styles.drop, dragOver && styles.dropActive)}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              const f = e.dataTransfer.files?.[0];
              if (f) void ingest(f);
            }}
          >
            <span className={styles.dropIcon} aria-hidden="true">
              <Icons.FileTextIcon />
            </span>
            <p className={styles.dropTitle}>Drag a CSV here</p>
            <p className={styles.dropHint}>
              The first row should be column headers, including a {matchLabel} column.
            </p>
            <Button variant="secondary" onClick={() => inputRef.current?.click()}>
              Choose file
            </Button>
            <input
              ref={inputRef}
              type="file"
              accept=".csv,text/csv"
              className={styles.fileInput}
              onChange={onFileInput}
              aria-label="Choose a CSV file"
            />
          </div>
          {fileError && (
            <p className={styles.error} role="alert">
              {fileError}
            </p>
          )}
        </div>
      )}

      {phase === "map" && parsed && (
        <div className={styles.mapStep}>
          {file && (
            <p className={styles.fileMeta}>
              <Icons.FileTextIcon />
              <span className={styles.fileName}>{file.name}</span>
              <span className={styles.fileSize}>{formatBytes(file.size)}</span>
            </p>
          )}

          <Field
            label={`Match ${entity === "contact" ? "contacts" : "accounts"} by`}
            hint={`The column holding each row's ${matchLabel}. Unmatched rows are skipped.`}
          >
            <Select
              value={matchColumn}
              options={headerOptions}
              onChange={(e) => changeMatch(e.target.value)}
            />
          </Field>

          <div
            className={styles.previewWrap}
            role="region"
            aria-label="CSV preview"
            tabIndex={0}
          >
            <table className={styles.preview}>
              <thead>
                <tr>
                  {parsed.headers.map((h, i) => (
                    <th key={i} scope="col">
                      {h || <span className={styles.muted}>—</span>}
                      {h === matchColumn && <span className={styles.keyTag}>key</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {parsed.rows.slice(0, PREVIEW_ROWS).map((row, r) => (
                  <tr key={r}>
                    {parsed.headers.map((_, c) => (
                      <td key={c}>{row[c] ?? ""}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className={styles.mapList}>
            <p className={styles.mapHeading}>Map columns to fields</p>
            {parsed.headers
              .filter((h) => h !== matchColumn)
              .map((h) => (
                <div key={h} className={styles.mapRow}>
                  <span className={styles.mapCol} title={h}>
                    {h || <span className={styles.muted}>(unnamed)</span>}
                  </span>
                  <Select
                    value={mapping[h] ?? NEW}
                    options={fieldOptions}
                    onChange={(e) => setMapping((m) => ({ ...m, [h]: e.target.value }))}
                    aria-label={`Map column ${h}`}
                  />
                </div>
              ))}
          </div>

          {serverError && (
            <p className={styles.error} role="alert">
              {serverError}
            </p>
          )}
        </div>
      )}

      {phase === "done" && result && (
        <div className={styles.doneStep}>
          <div className={styles.stats}>
            <Stat value={result.matched} label="Rows matched" />
            <Stat value={result.updated} label="Records updated" tone="success" />
            <Stat value={result.skipped} label="Rows skipped" tone={result.skipped > 0 ? "warn" : undefined} />
          </div>
          {result.created_fields.length > 0 && (
            <p className={styles.created}>
              <span className={styles.createdLabel}>New columns:</span>
              {result.created_fields.map((k) => (
                <Badge key={k} tone="info">
                  {k}
                </Badge>
              ))}
            </p>
          )}
          {result.matched === 0 && (
            <p className={styles.hint}>
              No rows matched an existing {entity} by {matchLabel}. Check that the match column holds
              values already in your CRM.
            </p>
          )}
        </div>
      )}

      {existing.loading && phase === "map" && (
        <p className={styles.loadingFields}>
          <Spinner size={14} /> Loading existing fields…
        </p>
      )}
    </Modal>
  );
}

function Stat({ value, label, tone }: { value: number; label: string; tone?: "success" | "warn" }) {
  return (
    <div className={styles.stat} data-tone={tone}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
