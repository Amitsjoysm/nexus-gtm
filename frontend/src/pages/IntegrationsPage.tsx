import { useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  Field,
  Icons,
  IconButton,
  Input,
  Select,
  useToast,
} from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type {
  CRMAccountInput,
  CRMSyncResponse,
  SEPPushResponse,
} from "@/lib/types";
import styles from "./IntegrationsPage.module.css";

const CRM_SOURCES: { value: "salesforce" | "hubspot"; label: string }[] = [
  { value: "salesforce", label: "Salesforce" },
  { value: "hubspot", label: "HubSpot" },
];

interface AccountRow {
  external_id: string;
  name: string;
  domain: string;
  industry: string;
  employee_count: string;
  country: string;
}

const EMPTY_ROW: AccountRow = {
  external_id: "",
  name: "",
  domain: "",
  industry: "",
  employee_count: "",
  country: "",
};

export function IntegrationsPage() {
  return (
    <div>
      <PageHeader
        title="Integrations"
        description="Connect your CRM and sales engagement tools. Import accounts and push contacts into sequences."
      />
      <div className={styles.grid}>
        <CrmCard />
        <SepCard />
      </div>
    </div>
  );
}

function CrmCard() {
  const api = useApiClient();
  const toast = useToast();
  const [source, setSource] = useState<"salesforce" | "hubspot">("salesforce");
  const [rows, setRows] = useState<AccountRow[]>([{ ...EMPTY_ROW }]);
  const [syncing, setSyncing] = useState(false);
  const [result, setResult] = useState<CRMSyncResponse | null>(null);

  function setRow(i: number, key: keyof AccountRow, value: string) {
    setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, [key]: value } : r)));
  }
  function addRow() {
    setRows((rs) => [...rs, { ...EMPTY_ROW }]);
  }
  function removeRow(i: number) {
    setRows((rs) => rs.filter((_, idx) => idx !== i));
  }

  const validRows = rows.filter((r) => r.external_id.trim() && r.name.trim());

  async function onSync(e: FormEvent) {
    e.preventDefault();
    setSyncing(true);
    setResult(null);
    try {
      const accounts: CRMAccountInput[] = validRows.map((r) => ({
        external_id: r.external_id.trim(),
        name: r.name.trim(),
        domain: r.domain.trim() || null,
        industry: r.industry.trim() || null,
        employee_count: r.employee_count.trim() ? Number(r.employee_count) : null,
        country: r.country.trim() || null,
      }));
      const res = await api.crmSync({ source, accounts });
      setResult(res);
      toast.success(
        "CRM synced",
        `${formatNumber(res.synced)} ${res.synced === 1 ? "account" : "accounts"} from ${res.source}.`,
      );
    } catch (err) {
      toast.error(
        "CRM sync failed",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSyncing(false);
    }
  }

  return (
    <Card padding="lg" className={styles.card}>
      <div className={styles.cardHead}>
        <span className={styles.cardIcon} aria-hidden="true">
          <Icons.BuildingIcon />
        </span>
        <div>
          <h2 className={styles.cardTitle}>CRM import</h2>
          <p className={styles.cardDesc}>
            Push accounts from your CRM into NEXUS. Existing accounts are matched and updated.
          </p>
        </div>
      </div>

      <form className={styles.form} onSubmit={onSync} noValidate>
        <Field label="Source">
          <Select
            value={source}
            onChange={(e) => setSource(e.target.value as "salesforce" | "hubspot")}
            options={CRM_SOURCES}
          />
        </Field>

        <div className={styles.rows}>
          {rows.map((r, i) => (
            <div key={i} className={styles.row}>
              <div className={styles.rowHead}>
                <span className={styles.rowIndex}>Account {i + 1}</span>
                {rows.length > 1 && (
                  <IconButton
                    label={`Remove account ${i + 1}`}
                    icon={<Icons.TrashIcon />}
                    size="sm"
                    onClick={() => removeRow(i)}
                  />
                )}
              </div>
              <div className={styles.grid2}>
                <Field label="External ID" required>
                  <Input
                    value={r.external_id}
                    onChange={(e) => setRow(i, "external_id", e.target.value)}
                    placeholder="0016A00000XyZ"
                    required
                  />
                </Field>
                <Field label="Name" required>
                  <Input
                    value={r.name}
                    onChange={(e) => setRow(i, "name", e.target.value)}
                    placeholder="Acme Corp"
                    required
                  />
                </Field>
                <Field label="Domain">
                  <Input
                    value={r.domain}
                    onChange={(e) => setRow(i, "domain", e.target.value)}
                    placeholder="acme.com"
                  />
                </Field>
                <Field label="Industry">
                  <Input
                    value={r.industry}
                    onChange={(e) => setRow(i, "industry", e.target.value)}
                    placeholder="Software"
                  />
                </Field>
                <Field label="Employees">
                  <Input
                    type="number"
                    min={0}
                    value={r.employee_count}
                    onChange={(e) => setRow(i, "employee_count", e.target.value)}
                    placeholder="250"
                  />
                </Field>
                <Field label="Country">
                  <Input
                    value={r.country}
                    onChange={(e) => setRow(i, "country", e.target.value)}
                    placeholder="United States"
                  />
                </Field>
              </div>
            </div>
          ))}
        </div>

        <div className={styles.actions}>
          <Button type="button" variant="ghost" size="sm" iconLeft={<Icons.PlusIcon />} onClick={addRow}>
            Add account
          </Button>
          <Button
            type="submit"
            iconLeft={<Icons.RefreshIcon />}
            loading={syncing}
            disabled={validRows.length === 0}
          >
            Sync {validRows.length || ""} {validRows.length === 1 ? "account" : "accounts"}
          </Button>
        </div>

        {result && (
          <div className={styles.result} role="status">
            <Badge tone="success" dot>
              Synced
            </Badge>
            <span>
              {formatNumber(result.synced)} {result.synced === 1 ? "account" : "accounts"} imported
              from {result.source}.
            </span>
          </div>
        )}
      </form>
    </Card>
  );
}

function SepCard() {
  const api = useApiClient();
  const toast = useToast();
  const [sequence, setSequence] = useState("");
  const [email, setEmail] = useState("");
  const [pushing, setPushing] = useState(false);
  const [result, setResult] = useState<SEPPushResponse | null>(null);

  async function onPush(e: FormEvent) {
    e.preventDefault();
    setPushing(true);
    setResult(null);
    try {
      const res = await api.sepPush({
        sequence: sequence.trim(),
        email: email.trim() || null,
      });
      setResult(res);
      if (res.ok) {
        toast.success("Pushed to sequence", `Queued on ${res.platform}.`);
      } else {
        toast.error("Push rejected", "The sequence platform declined the request.");
      }
    } catch (err) {
      toast.error(
        "Push failed",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setPushing(false);
    }
  }

  return (
    <Card padding="lg" className={styles.card}>
      <div className={styles.cardHead}>
        <span className={styles.cardIcon} aria-hidden="true">
          <Icons.SendIcon />
        </span>
        <div>
          <h2 className={styles.cardTitle}>Sequence push</h2>
          <p className={styles.cardDesc}>
            Enroll a contact into an Outreach or Salesloft sequence by email.
          </p>
        </div>
      </div>

      <form className={styles.form} onSubmit={onPush} noValidate>
        <Field label="Sequence" required>
          <Input
            value={sequence}
            onChange={(e) => setSequence(e.target.value)}
            placeholder="Q3 enterprise outbound"
            required
          />
        </Field>
        <Field label="Contact email" hint="The contact to enroll.">
          <Input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="buyer@acme.com"
          />
        </Field>
        <div className={styles.actions}>
          <Button
            type="submit"
            iconLeft={<Icons.SendIcon />}
            loading={pushing}
            disabled={!sequence.trim()}
          >
            Push to sequence
          </Button>
        </div>

        {result && (
          <div className={styles.result} role="status">
            <Badge tone={result.ok ? "success" : "danger"} dot>
              {result.ok ? "Queued" : "Rejected"}
            </Badge>
            <span>
              {result.ok
                ? `Contact enrolled on ${result.platform}.`
                : `${result.platform} declined the request.`}
            </span>
          </div>
        )}
      </form>
    </Card>
  );
}
