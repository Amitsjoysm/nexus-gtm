import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
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
import { ApiError } from "@/lib/api";
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
  const [query, setQuery] = useState("");
  const [industry, setIndustry] = useState(ALL);
  const [country, setCountry] = useState(ALL);
  const [source, setSource] = useState(ALL);
  const [minFit, setMinFit] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
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
      await api.exportAccounts();
    } catch (err) {
      toast.error("Couldn't export", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setExporting(false);
    }
  }

  const columns: Column<Account>[] = useMemo(
    () => [
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
    [busyId],
  );

  function filtered(rows: Account[]): Account[] {
    const q = query.trim().toLowerCase();
    return rows.filter((a) => {
      if (q && ![a.name, a.domain, a.industry, a.country].filter(Boolean).some((v) => v!.toLowerCase().includes(q)))
        return false;
      if (industry !== ALL && a.industry !== industry) return false;
      if (country !== ALL && a.country !== country) return false;
      if (source !== ALL && (a.source ?? "") !== source) return false;
      if (minFit > 0 && (a.fit_score ?? -1) < minFit) return false;
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
          const visible = filtered(rows);
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
                  <Select aria-label="Source" value={source} onChange={(e) => setSource(e.target.value)} options={opts(rows.map((a) => a.source ?? null))} />
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
                </div>
                <span className={styles.count}>
                  {visible.length} of {rows.length}
                </span>
              </div>
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
