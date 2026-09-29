import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  DataTable,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useCurrentUserId } from "@/app/useCurrentUserId";
import { ApiError } from "@/lib/api";
import { RecordImportModal } from "@/components/imports/RecordImportModal";
import { AddToListModal } from "@/components/lists/AddToListModal";
import { useSelection } from "@/hooks/useSelection";
import { formatNumber } from "@/lib/format";
import type { Account, AccountInput } from "@/lib/types";
import styles from "./AccountsPage.module.css";

function fitTone(score: number | null | undefined): "success" | "warning" | "danger" | "neutral" {
  if (score == null) return "neutral";
  if (score >= 70) return "success";
  if (score >= 40) return "warning";
  return "danger";
}

const ALL = "__all__";
const MINE = "__mine__";
const UNOWNED = "__unowned__";

/**
 * Where an account came from, in the words a rep uses. The stored `source` is a code per write path
 * (`discovery`, `auto_discovery`, `prospecting`, `lookalike`, `csv_import`, `crm:hubspot`), and
 * showing those raw made "which of these did the AI find?" a question about our internals.
 * `found` is the target of discovery's "View in Accounts" link.
 */
const SOURCE_GROUPS = [
  { value: "found", label: "Found by AI" },
  { value: "imported", label: "Imported from a file" },
  { value: "crm", label: "From your CRM" },
  { value: "manual", label: "Added by hand" },
] as const;
const FOUND_BY_AI = new Set(["discovery", "auto_discovery", "prospecting", "lookalike"]);

function sourceGroup(a: Account): string {
  const src = (a.source ?? "").toLowerCase();
  if (FOUND_BY_AI.has(src)) return "found";
  if (src === "csv_import") return "imported";
  if (src.startsWith("crm") || (!src && a.crm_source)) return "crm";
  return "manual";
}

const EMPTY_FORM = {
  name: "",
  domain: "",
  industry: "",
  employee_count: "",
  country: "",
  tech_stack: "",
};

export function AccountsPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const toast = useToast();

  const accounts = useApi<Account[]>((signal) => api.listAccounts(signal), []);
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [industry, setIndustry] = useState(ALL);
  const [country, setCountry] = useState(ALL);
  const [source, setSourceState] = useState(() => {
    const wanted = params.get("source") ?? "";
    return SOURCE_GROUPS.some((g) => g.value === wanted) ? wanted : ALL;
  });
  const [listOpen, setListOpen] = useState(false);
  const [minFit, setMinFit] = useState(0);
  const [ownerFilter, setOwnerFilter] = useState(ALL);
  const me = useCurrentUserId();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);

  async function pushToCrm(a: Account) {
    setBusyId(a.id);
    try {
      await api.crmPush(a.id);
      toast.success("Pushed to CRM", a.name);
      accounts.refetch();
    } catch (err) {
      toast.error("CRM push failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  /**
   * Soft delete, with the undo attached to the confirmation.
   *
   * The row is kept because signals, alerts, inbox tasks and cadence steps all reference it —
   * removing it would orphan the history that explains why anyone was ever contacted. Undo in the
   * toast rather than a separate "deleted items" screen: the mistake is noticed within seconds,
   * and a screen nobody visits is not a safety net.
   */
  async function removeAccount(a: Account) {
    setBusyId(a.id);
    try {
      await api.deleteAccount(a.id);
      accounts.refetch();
      toast.toast({
        tone: "success",
        title: "Account deleted",
        description: `${a.name} is hidden from your list. Its history is kept.`,
        action: {
          label: "Undo",
          onClick: async () => {
            try {
              await api.restoreAccount(a.id);
              accounts.refetch();
            } catch (err) {
              toast.error(
                "Couldn't restore",
                err instanceof ApiError ? err.detail : "Try again.",
              );
            }
          },
        },
      });
    } catch (err) {
      toast.error("Couldn't delete", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  async function exportAccounts() {
    setExporting(true);
    try {
      const { rows } = await api.exportAccounts();
      // No file rather than a header-only one: an empty CSV reads as a broken download.
      if (rows === 0)
        toast.toast({
          tone: "info",
          title: "Nothing to export",
          description: "This workspace has no accounts yet. Add or import some, then export.",
        });
    } catch (err) {
      toast.error("Couldn't export", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setExporting(false);
    }
  }

  /** Keep the address in step, so a filtered view is a link someone can be sent. */
  function setSource(next: string) {
    setSourceState(next);
    const updated = new URLSearchParams(params);
    if (next === ALL) updated.delete("source");
    else updated.set("source", next);
    setParams(updated, { replace: true });
  }

  const visible = useMemo(
    () => filtered(accounts.data ?? []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [accounts.data, query, industry, country, source, minFit, ownerFilter, me],
  );
  const visibleIds = useMemo(() => visible.map((a) => a.id), [visible]);
  const selection = useSelection(visibleIds);

  const columns: Column<Account>[] = useMemo(
    () => [
      {
        key: "select",
        width: "44px",
        align: "center",
        header: (
          <Checkbox
            label="Select every account shown"
            checked={selection.allVisible}
            indeterminate={selection.someVisible}
            disabled={visibleIds.length === 0}
            onChange={selection.toggleVisible}
          />
        ),
        render: (a) => (
          <Checkbox
            label={`Select ${a.name}`}
            checked={selection.selected.has(a.id)}
            onChange={() => selection.toggle(a.id)}
          />
        ),
      },
      {
        key: "name",
        header: "Account",
        sortValue: (a) => a.name,
        render: (a) => <span className={styles.name}>{a.name}</span>,
      },
      {
        key: "fit_score",
        header: "Fit",
        align: "right",
        sortValue: (a) => a.fit_score ?? null,
        render: (a) =>
          a.fit_score != null ? (
            <Badge tone={fitTone(a.fit_score)}>{a.fit_score}</Badge>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "owner",
        header: "Owner",
        hideOnMobile: true,
        sortValue: (a) => (a.owner_user_id ? (a.owner_name ?? "") : null),
        render: (a) =>
          !a.owner_user_id ? (
            <span className={styles.muted}>Unowned</span>
          ) : a.owner_user_id === me ? (
            "You"
          ) : (
            (a.owner_name ?? <span className={styles.muted}>Former member</span>)
          ),
      },
      {
        key: "industry",
        header: "Industry",
        hideOnMobile: true,
        sortValue: (a) => a.industry,
        render: (a) => a.industry ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "country",
        header: "Location",
        hideOnMobile: true,
        sortValue: (a) => a.country,
        render: (a) => a.country ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "employee_count",
        header: "Employees",
        align: "right",
        sortValue: (a) => a.employee_count ?? null,
        render: (a) => formatNumber(a.employee_count),
      },
      {
        key: "linkedin_url",
        header: "LinkedIn",
        hideOnMobile: true,
        align: "center",
        render: (a) =>
          a.linkedin_url ? (
            <a
              href={a.linkedin_url}
              target="_blank"
              rel="noreferrer noopener"
              className={styles.linkedin}
              onClick={(e) => e.stopPropagation()}
              title={`Open ${a.name} on LinkedIn`}
              aria-label={`Open ${a.name} on LinkedIn`}
            >
              in
            </a>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        render: (a) => (
          <span className={styles.rowActions} onClick={(e) => e.stopPropagation()}>
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.UsersIcon />}
              onClick={() => navigate(`/accounts/${a.id}`)}
              title="Open account & contacts"
              aria-label={`Open ${a.name}`}
            />
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.PlugIcon />}
              loading={busyId === a.id}
              onClick={() => pushToCrm(a)}
              title="Push to CRM"
              aria-label={`Push ${a.name} to CRM`}
            />
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.TrashIcon />}
              loading={busyId === a.id}
              onClick={() => removeAccount(a)}
              title="Delete account"
              aria-label={`Delete ${a.name}`}
            />
          </span>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busyId, me, selection, visibleIds],
  );

  function filtered(rows: Account[]): Account[] {
    const q = query.trim().toLowerCase();
    return rows.filter((a) => {
      if (q && ![a.name, a.domain, a.industry, a.country].filter(Boolean).some((v) => v!.toLowerCase().includes(q)))
        return false;
      if (industry !== ALL && a.industry !== industry) return false;
      if (country !== ALL && a.country !== country) return false;
      if (source !== ALL && sourceGroup(a) !== source) return false;
      if (minFit > 0 && (a.fit_score ?? -1) < minFit) return false;
      if (ownerFilter === MINE && (me === null || a.owner_user_id !== me)) return false;
      if (ownerFilter === UNOWNED && a.owner_user_id) return false;
      return true;
    });
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const input: AccountInput = {
        name: form.name.trim(),
        domain: form.domain.trim() || null,
        industry: form.industry.trim() || null,
        employee_count: form.employee_count ? Number(form.employee_count) : null,
        country: form.country.trim() || null,
        tech_stack: form.tech_stack
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      };
      const created = await api.createAccount(input);
      toast.success("Account created", created.name);
      setModalOpen(false);
      setForm(EMPTY_FORM);
      navigate(`/accounts/${created.id}`);
    } catch (err) {
      toast.error(
        "Couldn't create account",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Accounts"
        description="The companies in your territory, enriched and scored against your ICP."
        actions={
          <>
            <Button
              variant="secondary"
              iconLeft={<Icons.UploadIcon />}
              onClick={() => setImportOpen(true)}
            >
              Import
            </Button>
            <Button
              variant="secondary"
              iconLeft={<Icons.DownloadIcon />}
              onClick={exportAccounts}
              loading={exporting}
            >
              Export CSV
            </Button>
            <Button iconLeft={<Icons.PlusIcon />} onClick={() => setModalOpen(true)}>
              New account
            </Button>
          </>
        }
      />

      <AddToListModal
        open={listOpen}
        kind="account"
        accountIds={selection.ids}
        onClose={() => setListOpen(false)}
        onDone={selection.clear}
      />

      <RecordImportModal
        open={importOpen}
        entity="accounts"
        onClose={() => setImportOpen(false)}
        onImported={() => void accounts.refetch()}
      />

      <DataState
        state={accounts}
        skeleton={
          <Card padding="none">
            <DataTable<Account>
              columns={columns}
              rows={[]}
              getRowKey={(a) => a.id}
              loading
            />
          </Card>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.BuildingIcon />}
            title="No accounts yet"
            description="Add your first account to start tracking signals and building pipeline."
            action={
              <Button iconLeft={<Icons.PlusIcon />} onClick={() => setModalOpen(true)}>
                New account
              </Button>
            }
          />
        }
      >
        {(rows) => {
          const opts = (vals: (string | null)[]) => [
            { value: ALL, label: "All" },
            ...Array.from(new Set(vals.filter((v): v is string => !!v))).sort().map((v) => ({ value: v, label: v })),
          ];
          return (
            <>
              <div className={styles.toolbar}>
                <div className={styles.search}>
                  <span className={styles.searchIcon}>
                    <Icons.SearchIcon />
                  </span>
                  <input
                    type="search"
                    className={styles.searchInput}
                    placeholder="Search accounts…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    aria-label="Search accounts"
                  />
                </div>
                <div className={styles.filters}>
                  <Select aria-label="Industry" value={industry} onChange={(e) => setIndustry(e.target.value)} options={opts(rows.map((a) => a.industry))} />
                  <Select aria-label="Location" value={country} onChange={(e) => setCountry(e.target.value)} options={opts(rows.map((a) => a.country))} />
                  <Select
                    aria-label="Source"
                    value={source}
                    onChange={(e) => setSource(e.target.value)}
                    options={[{ value: ALL, label: "Any source" }, ...SOURCE_GROUPS]}
                  />
                  <Select
                    aria-label="Minimum fit"
                    value={String(minFit)}
                    onChange={(e) => setMinFit(Number(e.target.value))}
                    options={[
                      { value: "0", label: "Any fit" },
                      { value: "40", label: "Fit ≥ 40" },
                      { value: "60", label: "Fit ≥ 60" },
                      { value: "80", label: "Fit ≥ 80" },
                    ]}
                  />
                  <Select
                    aria-label="Owner"
                    value={ownerFilter}
                    onChange={(e) => setOwnerFilter(e.target.value)}
                    options={[
                      { value: ALL, label: "Any owner" },
                      { value: MINE, label: "My accounts" },
                      { value: UNOWNED, label: "Unowned" },
                    ]}
                  />
                </div>
                <span className={styles.count}>
                  {visible.length} of {rows.length}
                </span>
              </div>
              {selection.selected.size > 0 && (
                <div className={styles.selectionBar} role="region" aria-label="Selection actions">
                  <span className={styles.selectionCount}>
                    {formatNumber(selection.selected.size)} selected
                  </span>
                  <div className={styles.selectionActions}>
                    <Button size="sm" variant="ghost" onClick={selection.clear}>
                      Clear
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      iconLeft={<Icons.ListIcon />}
                      onClick={() => setListOpen(true)}
                    >
                      Add to list
                    </Button>
                  </div>
                </div>
              )}
              <Card padding="none">
                <DataTable<Account>
                  columns={columns}
                  rows={visible}
                  getRowKey={(a) => a.id}
                  onRowClick={(a) => navigate(`/accounts/${a.id}`)}
                  caption="Accounts"
                  empty={
                    <EmptyState
                      compact
                      icon={<Icons.SearchIcon />}
                      title="No matches"
                      description={`Nothing matches “${query}”.`}
                    />
                  }
                />
              </Card>
            </>
          );
        }}
      </DataState>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="New account"
        description="Add a company to your workspace."
        footer={
          <>
            <Button variant="secondary" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button form="new-account-form" type="submit" loading={submitting}>
              Create account
            </Button>
          </>
        }
      >
        <form id="new-account-form" className={styles.form} onSubmit={onSubmit} noValidate>
          <Field label="Company name" required>
            <Input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Acme Corp"
              required
            />
          </Field>
          <div className={styles.grid2}>
            <Field label="Domain">
              <Input
                value={form.domain}
                onChange={(e) => setForm({ ...form, domain: e.target.value })}
                placeholder="acme.com"
              />
            </Field>
            <Field label="Industry">
              <Input
                value={form.industry}
                onChange={(e) => setForm({ ...form, industry: e.target.value })}
                placeholder="Software"
              />
            </Field>
          </div>
          <div className={styles.grid2}>
            <Field label="Employees">
              <Input
                type="number"
                min={0}
                value={form.employee_count}
                onChange={(e) => setForm({ ...form, employee_count: e.target.value })}
                placeholder="250"
              />
            </Field>
            <Field label="Country">
              <Input
                value={form.country}
                onChange={(e) => setForm({ ...form, country: e.target.value })}
                placeholder="United States"
              />
            </Field>
          </div>
          <Field label="Tech stack" hint="Comma-separated, e.g. Snowflake, Segment.">
            <Input
              value={form.tech_stack}
              onChange={(e) => setForm({ ...form, tech_stack: e.target.value })}
              placeholder="Snowflake, Segment, Salesforce"
            />
          </Field>
        </form>
      </Modal>
    </div>
  );
}
