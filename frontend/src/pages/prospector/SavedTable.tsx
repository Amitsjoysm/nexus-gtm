import { Link } from "react-router-dom";
import { Badge, Checkbox, DataTable } from "@/components/ui";
import type { BadgeTone, Column } from "@/components/ui";
import type { ProspectView, SavedRow } from "@/lib/prospectorTypes";
import styles from "./Prospector.module.css";

const PROSPECTOR_SOURCE = "sourcing:prospector";
const RELEASED = new Set(["valid", "catch_all", "risky"]);

const STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  valid: { label: "Valid", tone: "success" },
  catch_all: { label: "Catch-all", tone: "info" },
  risky: { label: "Risky", tone: "warning" },
  unknown: { label: "Unknown", tone: "neutral" },
  invalid: { label: "Invalid", tone: "danger" },
};

export interface SavedTableProps {
  view: ProspectView;
  rows: SavedRow[];
  loading: boolean;
  selected: Set<string>;
  allVisible: boolean;
  someVisible: boolean;
  onToggle: (id: string) => void;
  onToggleAll: () => void;
}

/** The workspace's own records: real values, verification status, and the campaign hold. */
export function SavedTable({
  view, rows, loading, selected, allVisible, someVisible, onToggle, onToggleAll,
}: SavedTableProps) {
  const select: Column<SavedRow> = {
    key: "select",
    width: "44px",
    header: (
      <Checkbox label="Select every row on this page" checked={allVisible}
        indeterminate={someVisible && !allVisible} onChange={onToggleAll}
        disabled={rows.length === 0} />
    ),
    render: (row) => (
      <Checkbox label={`Select ${row.name}`} checked={selected.has(row.id)}
        onChange={() => onToggle(row.id)} />
    ),
  };

  const place = (row: SavedRow) => [row.city, row.region, row.country].filter(Boolean).join(", ");

  const contactColumns: Column<SavedRow>[] = [
    select,
    {
      key: "person",
      header: "Person",
      render: (row) => (
        <div className={styles.stack}>
          <Link className={styles.primaryLink} to={`/accounts/${row.account_id}`}>{row.name}</Link>
          <span className={styles.secondary}>
            {[row.title, row.seniority].filter(Boolean).join(" · ") || "No title"}
          </span>
        </div>
      ),
    },
    {
      key: "company",
      header: "Company",
      render: (row) => (
        <div className={styles.stack}>
          <span className={styles.primary}>{row.company_name}</span>
          <span className={styles.secondary}>
            {row.domain}
            {place(row) && <span className={styles.place}>{place(row)}</span>}
          </span>
        </div>
      ),
    },
    {
      key: "email",
      header: "Email",
      render: (row) => {
        if (!row.email) return <span className={styles.missing}>No email</span>;
        const status = STATUS[row.email_status ?? ""];
        const held = row.enrichment_source === PROSPECTOR_SOURCE
          && !RELEASED.has(row.email_status ?? "");
        return (
          <div className={styles.stack}>
            <span className={styles.mono}>{row.email}</span>
            <span className={styles.badges}>
              {status ? <Badge tone={status.tone}>{status.label}</Badge>
                : <Badge tone="neutral">Not checked</Badge>}
              {held && <Badge tone="warning">Verify before campaigns</Badge>}
            </span>
          </div>
        );
      },
    },
    {
      key: "phone",
      header: "Phone",
      hideOnMobile: true,
      render: (row) => row.phone ? <span className={styles.mono}>{row.phone}</span>
        : <span className={styles.missing}>—</span>,
    },
  ];

  const accountColumns: Column<SavedRow>[] = [
    select,
    {
      key: "company",
      header: "Company",
      render: (row) => (
        <div className={styles.stack}>
          <Link className={styles.primaryLink} to={`/accounts/${row.id}`}>{row.name}</Link>
          {row.domain && <span className={styles.secondary}>{row.domain}</span>}
        </div>
      ),
    },
    { key: "industry", header: "Industry",
      render: (row) => row.industry || <span className={styles.missing}>—</span> },
    {
      key: "size", header: "Employees", hideOnMobile: true,
      render: (row) => row.employee_count?.toLocaleString() ?? <span className={styles.missing}>—</span>,
    },
    {
      key: "location", header: "Location", hideOnMobile: true,
      render: (row) => place(row) || <span className={styles.missing}>—</span>,
    },
  ];

  return (
    <DataTable
      columns={view === "contacts" ? contactColumns : accountColumns}
      rows={rows}
      getRowKey={(row) => row.id}
      loading={loading}
      skeletonRows={8}
      density="compact"
      caption={view === "contacts" ? "Your contacts" : "Your accounts"}
    />
  );
}
