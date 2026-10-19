import { Link } from "react-router-dom";
import { Badge, Button, Checkbox, DataTable, Icons } from "@/components/ui";
import type { Column } from "@/components/ui";
import type { ProspectRow, ProspectView } from "@/lib/prospectorTypes";
import styles from "./Prospector.module.css";

export interface ResultsTableProps {
  view: ProspectView;
  rows: ProspectRow[];
  loading: boolean;
  selected: Set<string>;
  allVisible: boolean;
  someVisible: boolean;
  onToggle: (key: string) => void;
  onToggleAll: () => void;
  onReveal: (row: ProspectRow) => void;
}

/**
 * A provider value the workspace has not paid for: the partial mask (decision D7), a lock, and
 * words for a screen reader. The real value never reaches this component.
 */
function Masked({ mask, kind }: { mask: string; kind: string }) {
  return (
    <span className={styles.masked}>
      <Icons.LockIcon className={styles.lock} />
      <span className={styles.mono}>{mask}</span>
      <span className="sr-only">{kind} hidden until saved</span>
    </span>
  );
}

function Missing({ text }: { text: string }) {
  return <span className={styles.missing}>{text}</span>;
}

function place(row: ProspectRow): string {
  return [row.city, row.state, row.country].filter(Boolean).join(", ");
}

function fullName(row: ProspectRow): string {
  return `${row.first_name} ${row.last_name}`.trim() || "Unnamed";
}

/** Provider results: masked until saved, a Saved row carrying the workspace's own values. */
export function ResultsTable({
  view, rows, loading, selected, allVisible, someVisible, onToggle, onToggleAll, onReveal,
}: ResultsTableProps) {
  const select: Column<ProspectRow> = {
    key: "select",
    width: "44px",
    header: (
      <Checkbox label="Select every row on this page" checked={allVisible}
        indeterminate={someVisible && !allVisible} onChange={onToggleAll}
        disabled={rows.length === 0} />
    ),
    render: (row) => (
      <Checkbox
        label={`Select ${view === "contacts" ? fullName(row) : row.company_name}`}
        checked={selected.has(row.row_key)}
        onChange={() => onToggle(row.row_key)}
      />
    ),
  };

  const action: Column<ProspectRow> = {
    key: "action",
    header: <span className="sr-only">Actions</span>,
    align: "right",
    width: "96px",
    render: (row) => row.saved && row.record ? (
      <Link className={styles.savedLink}
        to={`/accounts/${row.record.account_id ?? row.record.id}`}>
        <Badge tone="success" icon={<Icons.CheckIcon />}>Saved</Badge>
      </Link>
    ) : (
      <Button variant="ghost" size="sm" onClick={() => onReveal(row)}
        aria-label={`Reveal ${view === "contacts" ? fullName(row) : row.company_name}`}>
        Reveal
      </Button>
    ),
  };

  const contactColumns: Column<ProspectRow>[] = [
    select,
    {
      key: "person",
      header: "Person",
      render: (row) => (
        <div className={styles.stack}>
          <span className={styles.primary}>{fullName(row)}</span>
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
          <span className={styles.primary}>{row.company_name || "Unknown company"}</span>
          <span className={styles.secondary}>
            {row.saved && row.record?.domain ? row.record.domain
              : row.domain_masked ? <Masked mask={row.domain_masked} kind="Website" /> : null}
            {place(row) && <span className={styles.place}>{place(row)}</span>}
          </span>
        </div>
      ),
    },
    {
      key: "reach",
      header: "Email & phone",
      render: (row) => (
        <div className={styles.stack}>
          {row.saved && row.record?.email
            ? <span className={styles.mono}>{row.record.email}</span>
            : row.has_email ? <Masked mask={row.email_masked} kind="Email" />
              : <Missing text="No email" />}
          {row.saved && row.record?.phone
            ? <span className={styles.mono}>{row.record.phone}</span>
            : row.has_direct_phone ? <Masked mask={row.phone_masked} kind="Phone number" />
              : <Missing text="No direct phone" />}
        </div>
      ),
    },
    action,
  ];

  const companyColumns: Column<ProspectRow>[] = [
    select,
    {
      key: "company",
      header: "Company",
      render: (row) => (
        <div className={styles.stack}>
          <span className={styles.primary}>{row.company_name || "Unknown company"}</span>
          {row.saved && row.record?.domain ? (
            <span className={styles.secondary}>{row.record.domain}</span>
          ) : row.domain_masked ? (
            <span className={styles.secondary}><Masked mask={row.domain_masked} kind="Website" /></span>
          ) : null}
        </div>
      ),
    },
    {
      key: "industry",
      header: "Industry & size",
      render: (row) => (
        <div className={styles.stack}>
          <span className={styles.primary}>{row.industry || "—"}</span>
          <span className={styles.secondary}>
            {row.employee_size_range ? `${row.employee_size_range} employees` : "Size unknown"}
          </span>
        </div>
      ),
    },
    {
      key: "location",
      header: "Location",
      hideOnMobile: true,
      render: (row) => place(row) || <Missing text="—" />,
    },
    {
      key: "linkedin",
      header: "LinkedIn",
      hideOnMobile: true,
      render: (row) => row.has_linkedin
        ? <Masked mask={row.linkedin_masked} kind="LinkedIn page" />
        : <Missing text="—" />,
    },
    action,
  ];

  return (
    <DataTable
      columns={view === "contacts" ? contactColumns : companyColumns}
      rows={rows}
      getRowKey={(row) => row.row_key}
      loading={loading}
      skeletonRows={8}
      density="compact"
      caption={view === "contacts" ? "People in the prospect database"
        : "Companies in the prospect database"}
    />
  );
}
