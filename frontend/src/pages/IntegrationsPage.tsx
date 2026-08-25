import { useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  ErrorState,
  Field,
  Icons,
  IconButton,
  Input,
  Modal,
  Select,
  Skeleton,
  useToast,
} from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { useApi } from "@/hooks/useApi";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type {
  CRMAccountInput,
  CRMConnection,
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
        <CrmConnectionCard />
        <SepCard />
      </div>
    </div>
  );
}

/**
 * Providers a workspace can pick.
 *
 * Salesforce was listed as "coming soon" and disabled while its adapter was a stub — accepting a
 * token then would have stored a secret that did nothing. The adapter is real now (OAuth2 + REST,
 * SOQL-backed fetch and push, `test_connection` that reports the instance it reached), the server
 * lists it in `LIVE_CRM_PROVIDERS`, and it has its own test file. The dropdown was the last thing
 * still saying otherwise, which is the "configured and doing nothing" state this codebase keeps
 * having to diagnose — only inverted: working and refusing to be offered.
 *
 * This list must stay in step with `crm_credentials.LIVE_CRM_PROVIDERS`; a provider offered here
 * and rejected there is a form that fails on submit.
 */
const CRM_PROVIDERS: { value: string; label: string; disabled?: boolean }[] = [
  { value: "hubspot", label: "HubSpot" },
  { value: "salesforce", label: "Salesforce" },
];

/** Badge tone + copy per connection state, so the header reads honestly at a glance. */
function statusChip(c: CRMConnection): { tone: "success" | "warning" | "danger" | "neutral"; text: string } {
  if (c.source === "env") return { tone: "neutral", text: "Using deployment default" };
  if (c.source === "none") return { tone: "neutral", text: "Not connected" };
  if (c.status === "connected") return { tone: "success", text: "Connected" };
  if (c.status === "error") return { tone: "danger", text: "Connection error" };
  return { tone: "warning", text: "Not verified" };
}

function CrmConnectionCard() {
  const api = useApiClient();
  const toast = useToast();
  const state = useApi<CRMConnection>((signal) => api.crmConnection(signal), []);
  const [provider, setProvider] = useState("hubspot");
  const [token, setToken] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);

  const conn = state.data;

  async function onSave(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      await api.setCrmConnection({
        provider,
        access_token: token.trim() || null,
        api_base: apiBase.trim(),
      });
      setToken("");
      state.refetch();
      toast.success("CRM saved", "Run a connection test to verify the credentials.");
    } catch (err) {
      toast.error("Couldn't save", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  async function onTest() {
    setTesting(true);
    try {
      const res = await api.testCrmConnection();
      state.refetch();
      if (res.ok) toast.success("Connection verified", res.label || res.detail);
      else toast.error("Connection failed", res.detail);
    } catch (err) {
      toast.error("Test failed", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setTesting(false);
    }
  }

  async function onClear() {
    try {
      await api.clearCrmConnection();
      setConfirmClear(false);
      setToken("");
      state.refetch();
      toast.success("CRM disconnected", "Syncs now use the deployment default.");
    } catch (err) {
      toast.error("Couldn't disconnect", err instanceof ApiError ? err.detail : "Please try again.");
    }
  }

  const chip = conn ? statusChip(conn) : null;

  return (
    <Card padding="lg" className={styles.card}>
      <div className={styles.cardHead}>
        <span className={styles.cardIcon} aria-hidden="true">
          <Icons.PlugIcon />
        </span>
        <div className={styles.cardHeadText}>
          <h2 className={styles.cardTitle}>CRM connection</h2>
          <p className={styles.cardDesc}>
            Connect your own CRM. Credentials are encrypted and never shown again after saving.
          </p>
        </div>
        {chip && (
          <Badge tone={chip.tone} dot>
            {chip.text}
          </Badge>
        )}
      </div>

      {state.loading && (
        <div className={styles.form} aria-busy="true">
          <Skeleton height={60} />
          <Skeleton height={60} />
          <Skeleton height={36} width="60%" />
        </div>
      )}

      {state.error && (
        <ErrorState
          title="Couldn't load the CRM connection"
          message={state.error.detail}
          onRetry={state.refetch}
          compact
        />
      )}

      {conn && !state.loading && (
        <>
          <form className={styles.form} onSubmit={onSave} noValidate>
            <Field label="Provider">
              <Select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                options={CRM_PROVIDERS}
              />
            </Field>

            <Field
              label="Private app access token"
              required={!conn.has_credentials}
              hint={
                conn.has_credentials
                  ? "A token is saved. Leave this blank to keep it."
                  : "In HubSpot: Settings → Integrations → Private Apps."
              }
            >
              <Input
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={conn.has_credentials ? "••••••••  (saved)" : "pat-na1-…"}
              />
            </Field>

            <details className={styles.advanced}>
              <summary className={styles.summary}>Advanced</summary>
              <Field label="API base URL" hint="Only for a regional host or a proxy.">
                <Input
                  value={apiBase}
                  onChange={(e) => setApiBase(e.target.value)}
                  placeholder={conn.api_base || "https://api.hubapi.com"}
                />
              </Field>
            </details>

            {conn.last_error && (
              <p className={styles.errorNote} role="status">
                {conn.last_error}
              </p>
            )}
            {conn.status === "connected" && conn.verified_at && (
              <p className={styles.okNote} role="status">
                Last verified {new Date(conn.verified_at).toLocaleString()}.
              </p>
            )}

            <div className={styles.actions}>
              <div className={styles.actionGroup}>
                <Button type="submit" loading={saving}>
                  Save credentials
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  iconLeft={<Icons.ShieldCheckIcon />}
                  loading={testing}
                  onClick={onTest}
                >
                  Test connection
                </Button>
              </div>
              {conn.source === "tenant" && (
                <Button type="button" variant="ghost" onClick={() => setConfirmClear(true)}>
                  Disconnect
                </Button>
              )}
            </div>
          </form>

          <details className={styles.advanced}>
            <summary className={styles.summary}>Import accounts manually</summary>
            <ManualImportForm />
          </details>
        </>
      )}

      <Modal
        open={confirmClear}
        onClose={() => setConfirmClear(false)}
        title="Disconnect CRM?"
        description="Stored credentials are deleted. Syncs fall back to the deployment default, which may point at a different CRM."
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmClear(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={onClear}>
              Disconnect CRM
            </Button>
          </>
        }
      >
        <p>You will need the access token again to reconnect.</p>
      </Modal>
    </Card>
  );
}

function ManualImportForm() {
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
