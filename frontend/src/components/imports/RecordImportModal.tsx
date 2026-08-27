import { useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Button,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Spinner,
} from "@/components/ui";
import type { SelectOption } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { parseCsv } from "@/lib/csv";
import type { ParsedCsv } from "@/lib/csv";
import type { ImportFields, RecordImportResult } from "@/lib/types";
import styles from "./RecordImportModal.module.css";

/**
 * Bring an existing list in — by CSV upload, or pulled from the connected CRM.
 *
 * Distinct from `ImportCsvModal`, which posts to `/custom-fields/import`: that one *annotates* rows
 * that already match and skips the rest, so a team arriving with a list of companies had no way to
 * get it in. This one **creates**.
 *
 * The mapping is explicit rather than guessed from headers. A "Company" column of parent-company
 * names silently becoming the account name for every subsidiary is the kind of error nobody finds
 * for a month.
 */

const PREVIEW_ROWS = 6;
const IGNORE = "__ignore__";

export type ImportEntity = "accounts" | "contacts";

interface Props {
  open: boolean;
  entity: ImportEntity;
  onClose: () => void;
  /** Fired after a successful import so the caller can refetch. */
  onImported?: (result: RecordImportResult) => void;
}

/** Human labels for the server's field names. The server is the source of truth for WHICH fields
 * exist; this only decides how they read. An unlabelled field falls back to its raw name, so a
 * field added server-side appears immediately rather than vanishing from the picker. */
const LABELS: Record<string, string> = {
  name: "Company name",
  domain: "Website / domain",
  industry: "Industry",
  country: "Country",
  region: "State / province",
  postal_code: "ZIP / postal code",
  employee_count: "Employee count",
  annual_revenue: "Annual revenue",
  full_name: "Full name",
  email: "Email",
  title: "Job title",
  seniority: "Seniority",
  phone: "Phone",
  linkedin_url: "LinkedIn URL",
  account_domain: "Company website / domain",
  account_name: "Company name",
};

/** Fields that identify the record. At least one must be mapped or the import cannot match
 * anything, and the server would skip every row while reporting it politely. Catching it here
 * means the operator fixes it before uploading rather than after. */
const REQUIRED_ONE_OF: Record<ImportEntity, string[]> = {
  accounts: ["name", "domain"],
  contacts: ["email"],
};

/** Header text -> field name, for the initial guess. Only a starting point: every row stays
 * editable, because a wrong guess the operator does not notice is worse than no guess. */
function guessField(header: string, allowed: string[]): string {
  const h = header.trim().toLowerCase().replace(/[^a-z0-9]+/g, "");
  const rules: [RegExp, string][] = [
    [/^(company|companyname|account|accountname|organisation|organization|org)$/, "name"],
    [/^(website|url|domain|companydomain|websiteurl|companyurl)$/, "domain"],
    [/^(industry|sector|vertical)$/, "industry"],
    [/^(country|countryname)$/, "country"],
    [/^(state|province|region|stateprovince)$/, "region"],
    [/^(zip|zipcode|postal|postalcode|postcode)$/, "postal_code"],
    [/^(employees|employeecount|headcount|staff|size)$/, "employee_count"],
    [/^(revenue|annualrevenue|arr|turnover)$/, "annual_revenue"],
    [/^(name|fullname|contactname|person)$/, "full_name"],
    [/^(email|emailaddress|workemail)$/, "email"],
    [/^(title|jobtitle|role|position)$/, "title"],
    [/^(phone|phonenumber|mobile|telephone)$/, "phone"],
    [/^(linkedin|linkedinurl|liurl|profile)$/, "linkedin_url"],
  ];
  for (const [pattern, field] of rules) {
    if (pattern.test(h) && allowed.includes(field)) return field;
  }
  return IGNORE;
}

export function RecordImportModal({ open, entity, onClose, onImported }: Props) {
  const api = useApiClient();
  const fileInput = useRef<HTMLInputElement>(null);

  const [fields, setFields] = useState<ImportFields | null>(null);
  const [parsed, setParsed] = useState<ParsedCsv | null>(null);
  const [fileName, setFileName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<RecordImportResult | null>(null);
  const [crmLimit, setCrmLimit] = useState(100);

  const allowed = useMemo(
    () => (entity === "accounts" ? fields?.account_fields : fields?.contact_fields) ?? [],
    [fields, entity],
  );

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api
      .importableFields()
      .then((f) => {
        if (!cancelled) {
          setFields(f);
          setCrmLimit(f.default_limit);
        }
      })
      .catch(() => {
        /* The picker degrades to the fields we know; it must not block the upload entirely. */
      });
    return () => {
      cancelled = true;
    };
  }, [api, open]);

  // Clear everything when the modal closes, so reopening never shows the previous file's mapping
  // against a new file's headers — a mismatch that silently maps the wrong columns.
  useEffect(() => {
    if (open) return;
    setParsed(null);
    setFile(null);
    setFileName("");
    setMapping({});
    setError("");
    setResult(null);
  }, [open]);

  async function onFile(chosen: File | undefined) {
    if (!chosen) return;
    setError("");
    setResult(null);
    const max = fields?.max_upload_bytes ?? 20 * 1024 * 1024;
    if (chosen.size > max) {
      setError(`That file is ${(chosen.size / 1024 / 1024).toFixed(1)} MB. The limit is ${Math.round(max / 1024 / 1024)} MB.`);
      return;
    }
    const text = await chosen.text();
    const csv = parseCsv(text, { maxRows: PREVIEW_ROWS });
    if (!csv.headers.length) {
      setError("That file has no header row, so there are no columns to map.");
      return;
    }
    setFile(chosen);
    setFileName(chosen.name);
    setParsed(csv);
    setMapping(
      Object.fromEntries(csv.headers.map((h) => [h, guessField(h, allowed)])),
    );
  }

  const mapped = useMemo(
    () => Object.entries(mapping).filter(([, v]) => v && v !== IGNORE),
    [mapping],
  );

  const identityMissing = useMemo(() => {
    const chosen = new Set(mapped.map(([, v]) => v));
    return !REQUIRED_ONE_OF[entity].some((f) => chosen.has(f));
  }, [mapped, entity]);

  // A field mapped twice writes one column over the other and the operator sees neither error nor
  // the data they expected.
  const duplicates = useMemo(() => {
    const counts = new Map<string, number>();
    for (const [, field] of mapped) counts.set(field, (counts.get(field) ?? 0) + 1);
    return [...counts.entries()].filter(([, n]) => n > 1).map(([f]) => f);
  }, [mapped]);

  const fieldOptions: SelectOption[] = useMemo(
    () => [
      { value: IGNORE, label: "Keep as extra data" },
      ...allowed.map((f) => ({ value: f, label: LABELS[f] ?? f })),
    ],
    [allowed],
  );

  async function runCsvImport() {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const payload = Object.fromEntries(mapped);
      const res =
        entity === "accounts"
          ? await api.importAccountsCsv({ mapping: payload, file })
          : await api.importContactsCsv({ mapping: payload, file });
      setResult(res);
      onImported?.(res);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The import failed. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  async function runCrmImport() {
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const res =
        entity === "accounts"
          ? await api.importAccountsFromCrm(crmLimit)
          : await api.importContactsFromCrm(crmLimit);
      setResult(res);
      onImported?.(res);
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message
          : "Could not read from the CRM. Nothing was changed.",
      );
    } finally {
      setBusy(false);
    }
  }

  const noun = entity === "accounts" ? "accounts" : "contacts";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Import ${noun}`}
      size="lg"
    >
      <div className={styles.body}>
        {result ? (
          <div className={styles.result} role="status">
            <div className={styles.resultRow}>
              <Badge tone="success">{result.created} created</Badge>
              <Badge tone="neutral">{result.updated} updated</Badge>
              {result.skipped > 0 && <Badge tone="warning">{result.skipped} skipped</Badge>}
              <span className={styles.muted}>of {result.total_rows} rows</span>
            </div>
            {result.errors.length > 0 && (
              /* Skipped rows are shown, never swallowed: a silent drop reads as data loss and the
                 operator has no way to find which row it was. */
              <details className={styles.errors}>
                <summary>{result.errors.length} row(s) could not be imported</summary>
                <ul>
                  {result.errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </details>
            )}
            <div className={styles.actions}>
              <Button
                variant="secondary"
                onClick={() => {
                  setResult(null);
                  setParsed(null);
                  setFile(null);
                  setFileName("");
                }}
              >
                Import more
              </Button>
              <Button onClick={onClose}>Done</Button>
            </div>
          </div>
        ) : (
          <>
            <section className={styles.section}>
              <h3 className={styles.heading}>From a CSV file</h3>
              <input
                ref={fileInput}
                type="file"
                accept=".csv,text/csv"
                className={styles.hiddenInput}
                onChange={(e) => void onFile(e.target.files?.[0])}
              />
              <div className={styles.fileRow}>
                <Button variant="secondary" onClick={() => fileInput.current?.click()}>
                  <Icons.UploadIcon aria-hidden />
                  Choose file
                </Button>
                {fileName && <span className={styles.muted}>{fileName}</span>}
              </div>

              {parsed && (
                <>
                  <p className={styles.hint}>
                    Match each column to a field. Anything left as{" "}
                    <strong>Keep as extra data</strong> is stored on the record rather than dropped.
                  </p>
                  <div className={styles.mapGrid}>
                    {parsed.headers.map((header, columnIndex) => (
                      <Field key={header} label={header}>
                        <Select
                          value={mapping[header] ?? IGNORE}
                          options={fieldOptions}
                          onChange={(e) =>
                            setMapping((m) => ({ ...m, [header]: e.target.value }))
                          }
                        />
                        <span className={styles.sample}>
                          {parsed.rows[0]?.[columnIndex] || <em>empty</em>}
                        </span>
                      </Field>
                    ))}
                  </div>

                  {identityMissing && (
                    <p className={styles.warn} role="alert">
                      {entity === "accounts"
                        ? "Map at least a company name or a website — without one, no row can be matched or created."
                        : "Map an email column — a contact is identified by their email address."}
                    </p>
                  )}
                  {duplicates.length > 0 && (
                    <p className={styles.warn} role="alert">
                      {duplicates.map((d) => LABELS[d] ?? d).join(", ")} is mapped more than once.
                      One column would overwrite the other.
                    </p>
                  )}

                  <div className={styles.actions}>
                    <Button variant="ghost" onClick={onClose} disabled={busy}>
                      Cancel
                    </Button>
                    <Button
                      onClick={() => void runCsvImport()}
                      disabled={busy || identityMissing || duplicates.length > 0}
                    >
                      {busy ? <Spinner size={16} /> : `Import ${noun}`}
                    </Button>
                  </div>
                </>
              )}
            </section>

            <section className={styles.section}>
              <h3 className={styles.heading}>From your CRM</h3>
              <p className={styles.hint}>
                Pulls from the CRM connected to this workspace. Imported {noun} start being
                refreshed, so bring across a batch you intend to work.
              </p>
              <div className={styles.crmRow}>
                <Field label="How many to import">
                  <Input
                    type="number"
                    min={1}
                    max={5000}
                    value={String(crmLimit)}
                    onChange={(e) =>
                      setCrmLimit(Math.max(1, Math.min(5000, Number(e.target.value) || 1)))
                    }
                  />
                </Field>
                <Button variant="secondary" onClick={() => void runCrmImport()} disabled={busy}>
                  {busy ? <Spinner size={16} /> : `Import from CRM`}
                </Button>
              </div>
            </section>
          </>
        )}

        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
