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
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);

  const columns: Column<Account>[] = useMemo(
    () => [
      {
        key: "name",
        header: "Account",
        render: (a) => <span className={styles.name}>{a.name}</span>,
      },
      {
        key: "domain",
        header: "Domain",
        hideOnMobile: true,
        render: (a) =>
          a.domain ? a.domain : <span className={styles.muted}>—</span>,
      },
      {
        key: "industry",
        header: "Industry",
        hideOnMobile: true,
        render: (a) => a.industry ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "employee_count",
        header: "Employees",
        align: "right",
        render: (a) => formatNumber(a.employee_count),
      },
      {
        key: "tech_stack",
        header: "Tech",
        hideOnMobile: true,
        render: (a) =>
          a.tech_stack.length ? (
            <span className={styles.tech}>
              {a.tech_stack.slice(0, 3).map((t) => (
                <Badge key={t} tone="neutral">
                  {t}
                </Badge>
              ))}
              {a.tech_stack.length > 3 && (
                <Badge tone="neutral">+{a.tech_stack.length - 3}</Badge>
              )}
            </span>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
    ],
    [],
  );

  function filtered(rows: Account[]): Account[] {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((a) =>
      [a.name, a.domain, a.industry, a.country]
        .filter(Boolean)
        .some((v) => v!.toLowerCase().includes(q)),
    );
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
          <Button iconLeft={<Icons.PlusIcon />} onClick={() => setModalOpen(true)}>
            New account
          </Button>
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
