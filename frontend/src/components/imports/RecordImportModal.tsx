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
import { cn } from "@/lib/cn";
import { parseCsv } from "@/lib/csv";
import { formatNumber } from "@/lib/format";
import type { ParsedCsv } from "@/lib/csv";
import type { ImportFields, RecordImportResult } from "@/lib/types";
import styles from "./RecordImportModal.module.css";

/** "1.2 MB". Sizes are shown so an operator can tell a 40-row test file from the real export. */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

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
  tech_stack: "Technologies used",
  crm_id: "CRM record ID",
  crm_source: "CRM name",
  full_name: "Full name",
  email: "Email",
  title: "Job title",
  seniority: "Seniority",
  phone: "Phone",
  linkedin_url: "LinkedIn URL",
  account_domain: "Company website / domain",
  account_name: "Company name",
};

/** Fields whose format is not obvious from the label. Shown under the picker once chosen, because
 *  an operator who separates technologies with a slash gets one long string and no warning. */
const FORMAT_HINT: Record<string, string> = {
  tech_stack: "Separate several with commas or semicolons. Added to what is already known.",
  crm_id: "Matches the record in your CRM so a sync updates it instead of creating a duplicate.",
  annual_revenue: "Whole numbers. $25,000,000 and 25000000 both work.",
  employee_count: "Whole numbers.",
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
    // A CRM export is the commonest shape of file here, and both of these were previously guessed
    // as "keep as extra data" — which quietly cost the relevance engine the stack and the CRM sync
    // its match key.
    [/^(technologies|techstack|tech|stack|technology|tools)$/, "tech_stack"],
    [/^(crmid|sfid|salesforceid|hubspotid|recordid|externalid|accountid)$/, "crm_id"],
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
  /** Only what the OPERATOR chose. The guess is derived, so a file picked before the field list
   *  arrives still gets guessed once it does — previously the guess ran in the file handler
   *  against an empty field list and every column silently read "Keep as extra data". */
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<RecordImportResult | null>(null);
  const [crmLimit, setCrmLimit] = useState(100);

  const allowed = useMemo(
    () => (entity === "accounts" ? fields?.account_fields : fields?.contact_fields) ?? [],
    [fields, entity],
  );

  /** The workspace's own defined fields. Mapping a column onto one of these is what puts the value
   *  on the field the workspace's filters read, instead of under the raw CSV header. */
  const customFields = useMemo(
    () =>
      (entity === "accounts" ? fields?.account_custom_fields : fields?.contact_custom_fields) ?? [],
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
    setOverrides({});
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
    if (!/\.csv$/i.test(chosen.name) && chosen.type && !chosen.type.includes("csv")) {
      setError(`${chosen.name} is not a CSV. Export the sheet as CSV and try again.`);
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
    setOverrides({});
  }

  function clearFile() {
    setFile(null);
    setFileName("");
    setParsed(null);
    setOverrides({});
    setError("");
  }

  /** The operator's choice where they made one, the guess otherwise. */
  const mapping = useMemo(() => {
    const out: Record<string, string> = {};
    for (const header of parsed?.headers ?? []) {
      out[header] = overrides[header] ?? guessField(header, allowed);
    }
    return out;
  }, [parsed, overrides, allowed]);

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
      ...allowed.map((f) => ({
        value: f,
        label: LABELS[f] ?? f,
        group: entity === "accounts" ? "Company fields" : "Contact fields",
      })),
      // The workspace's OWN fields, grouped separately: a field somebody defined and a field we
      // ship are different things, and a flat list of thirty makes neither findable.
      ...customFields.map((c) => ({
        value: c.target,
        label: c.label,
        group: "Your workspace's fields",
      })),
    ],
    [allowed, customFields, entity],
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
              {file ? (
                <div className={styles.file}>
                  <span className={styles.fileIcon} aria-hidden="true">
                    <Icons.CheckIcon />
                  </span>
                  <span className={styles.fileText}>
                    <span className={styles.fileName}>{fileName}</span>
                    <span className={styles.muted}>
                      {parsed?.headers.length ?? 0} columns · {formatBytes(file.size)}
                    </span>
                  </span>
                  <Button size="sm" variant="ghost" onClick={clearFile} disabled={busy}>
                    Choose a different file
                  </Button>
                </div>
              ) : (
                <div
                  className={cn(styles.drop, dragging && styles.dropActive)}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragging(true);
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDragging(false);
                    void onFile(e.dataTransfer.files?.[0]);
                  }}
                >
                  <span className={styles.dropIcon} aria-hidden="true">
                    <Icons.UploadIcon />
                  </span>
                  <span className={styles.dropTitle}>Drop a CSV here</span>
                  <Button
                    variant="secondary"
                    size="sm"
                    iconLeft={<Icons.UploadIcon />}
                    onClick={() => fileInput.current?.click()}
                  >
                    Choose a file
                  </Button>
                  <span className={styles.muted}>
                    Up to {Math.round((fields?.max_upload_bytes ?? 20971520) / 1024 / 1024)} MB and{" "}
                    {formatNumber(fields?.max_rows ?? 50000)} rows. You match the columns next.
                  </span>
                </div>
              )}

              {parsed && (
                <>
                  <p className={styles.hint}>
                    Match each column to a field. Anything left as{" "}
                    <strong>Keep as extra data</strong> is stored on the record under its own column
                    name rather than dropped.
                  </p>
                  <div className={styles.mapGrid}>
                    {parsed.headers.map((header, columnIndex) => {
                      const target = mapping[header] ?? IGNORE;
                      return (
                        <Field key={header} label={header} hint={FORMAT_HINT[target]}>
                          <Select
                            value={target}
                            options={fieldOptions}
                            onChange={(e) =>
                              setOverrides((m) => ({ ...m, [header]: e.target.value }))
                            }
                          />
                          <span className={styles.sample}>
                            {parsed.rows[0]?.[columnIndex] || <em>empty</em>}
                          </span>
                        </Field>
                      );
                    })}
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
