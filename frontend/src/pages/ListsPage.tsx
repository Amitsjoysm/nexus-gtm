import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
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
import { formatNumber, timeAgo } from "@/lib/format";
import type { Account, ListFilter, ProspectList } from "@/lib/types";
import styles from "./ListsPage.module.css";

const EMPTY_FORM = {
  industries: "",
  countries: "",
  min_employees: "",
  max_employees: "",
  min_composite: "0",
};

const splitCsv = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
const numOrNull = (s: string) => (s.trim() === "" ? null : Number(s));

export function ListsPage() {
  const api = useApiClient();
  const toast = useToast();

  const [form, setForm] = useState(EMPTY_FORM);
  const [results, setResults] = useState<Account[] | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [savedNonce, setSavedNonce] = useState(0);

  const [modalOpen, setModalOpen] = useState(false);
  const [listName, setListName] = useState("");
  const [saving, setSaving] = useState(false);

  function buildFilter(): ListFilter {
    return {
      industries: splitCsv(form.industries),
      countries: splitCsv(form.countries),
      min_employees: numOrNull(form.min_employees),
      max_employees: numOrNull(form.max_employees),
      min_composite: Number(form.min_composite) || null,
    };
  }

  async function onPreview(e?: FormEvent) {
    e?.preventDefault();
    setPreviewing(true);
    setPreviewError(null);
    try {
      const rows = await api.previewList(buildFilter());
      setResults(rows);
    } catch (err) {
      setPreviewError(err instanceof ApiError ? err.detail : "Couldn't preview the list.");
      setResults(null);
    } finally {
      setPreviewing(false);
    }
  }

  function reset() {
    setForm(EMPTY_FORM);
    setResults(null);
    setPreviewError(null);
  }

  async function onSave(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const built = await api.buildList(listName.trim(), buildFilter());
      toast.success(
        "List saved",
        `“${built.name}” has ${formatNumber(built.accounts)} ${built.accounts === 1 ? "account" : "accounts"}.`,
      );
      setModalOpen(false);
      setListName("");
      setSavedNonce((n) => n + 1);
    } catch (err) {
      toast.error(
        "Couldn't save list",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSaving(false);
    }
  }

  const columns: Column<Account>[] = useMemo(
    () => [
      { key: "name", header: "Account", render: (a) => <span className={styles.name}>{a.name}</span> },
      {
        key: "industry",
        header: "Industry",
        hideOnMobile: true,
        render: (a) => a.industry ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "country",
        header: "Country",
        hideOnMobile: true,
        render: (a) => a.country ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "employee_count",
        header: "Employees",
        align: "right",
        render: (a) => formatNumber(a.employee_count),
      },
    ],
    [],
  );

  const fit = Number(form.min_composite);

  return (
    <div>
      <PageHeader
        title="Lists"
        description="Build a segment from your filters, preview the matches, then save it as a working list."
      />

      <div className={styles.layout}>
        <Card padding="lg" className={styles.filters}>
          <form className={styles.form} onSubmit={onPreview} noValidate>
            <h2 className={styles.formTitle}>Filters</h2>
            <Field label="Industries" hint="Comma-separated. Leave blank for any.">
              <Input
                value={form.industries}
                onChange={(e) => setForm({ ...form, industries: e.target.value })}
                placeholder="Software, Fintech"
              />
            </Field>
            <Field label="Countries" hint="Comma-separated. Leave blank for any.">
              <Input
                value={form.countries}
                onChange={(e) => setForm({ ...form, countries: e.target.value })}
                placeholder="United States"
              />
            </Field>
            <div className={styles.grid2}>
              <Field label="Min employees">
                <Input
                  type="number"
                  min={0}
                  value={form.min_employees}
                  onChange={(e) => setForm({ ...form, min_employees: e.target.value })}
                  placeholder="50"
                />
              </Field>
              <Field label="Max employees">
                <Input
                  type="number"
                  min={0}
                  value={form.max_employees}
                  onChange={(e) => setForm({ ...form, max_employees: e.target.value })}
                  placeholder="5000"
                />
              </Field>
            </div>
            <Field label={`Minimum fit score: ${fit}`} hint="Composite account score, 0–100.">
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={form.min_composite}
                onChange={(e) => setForm({ ...form, min_composite: e.target.value })}
                className={styles.range}
                aria-label="Minimum fit score"
              />
            </Field>
            <div className={styles.formActions}>
              <Button type="submit" iconLeft={<Icons.SearchIcon />} loading={previewing}>
                Preview
              </Button>
              <Button type="button" variant="ghost" onClick={reset}>
                Reset
              </Button>
            </div>
          </form>
        </Card>

        <div className={styles.results}>
          <div className={styles.resultsHead}>
            <div>
              <h2 className={styles.resultsTitle}>Matches</h2>
              {results && (
                <p className={styles.resultsCount}>
                  {formatNumber(results.length)} {results.length === 1 ? "account" : "accounts"}
                </p>
              )}
            </div>
            <Button
              variant="secondary"
              iconLeft={<Icons.ListIcon />}
              disabled={!results || results.length === 0}
              onClick={() => setModalOpen(true)}
            >
              Save as list
            </Button>
          </div>

          {previewing ? (
            <Card padding="none">
              <DataTable<Account> columns={columns} rows={[]} getRowKey={(a) => a.id} loading />
            </Card>
          ) : previewError ? (
            <Card padding="lg">
              <EmptyState
                compact
                icon={<Icons.AlertTriangleIcon />}
                title="Couldn't preview"
                description={previewError}
                action={
                  <Button variant="secondary" onClick={() => onPreview()}>
                    Try again
                  </Button>
                }
              />
            </Card>
          ) : results === null ? (
            <Card padding="lg">
              <EmptyState
                icon={<Icons.ListIcon />}
                title="No preview yet"
                description="Set your filters and run a preview to see which accounts match."
              />
            </Card>
          ) : results.length === 0 ? (
            <Card padding="lg">
              <EmptyState
                compact
                icon={<Icons.SearchIcon />}
                title="No matches"
                description="No accounts match these filters. Try loosening them."
              />
            </Card>
          ) : (
            <Card padding="none">
              <DataTable<Account>
                columns={columns}
                rows={results}
                getRowKey={(a) => a.id}
                caption="Matching accounts"
              />
            </Card>
          )}
        </div>
      </div>

      <SavedLists refreshKey={savedNonce} />

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="Save as list"
        description={
          results
            ? `${formatNumber(results.length)} ${results.length === 1 ? "account" : "accounts"} will be added.`
            : undefined
        }
        footer={
          <>
            <Button variant="secondary" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button form="save-list-form" type="submit" loading={saving} disabled={!listName.trim()}>
              Save list
            </Button>
          </>
        }
      >
        <form id="save-list-form" onSubmit={onSave} noValidate>
          <Field label="List name" required>
            <Input
              value={listName}
              onChange={(e) => setListName(e.target.value)}
              placeholder="Enterprise fintech, US"
              autoFocus
              required
            />
          </Field>
        </form>
      </Modal>
    </div>
  );
}

/** The segments already saved in this workspace; each one is a campaign waiting to launch. */
function SavedLists({ refreshKey }: { refreshKey: number }) {
  const api = useApiClient();
  const navigate = useNavigate();
  const lists = useApi<ProspectList[]>((signal) => api.listSavedLists(signal), [refreshKey]);

  return (
    <Card padding="md" className={styles.saved}>
      <div className={styles.savedHead}>
        <h3 className={styles.savedTitle}>Saved lists</h3>
        <Button variant="ghost" size="sm" onClick={() => navigate("/campaigns")}>
          Launch a campaign
        </Button>
      </div>
      <DataState
        state={lists}
        errorTitle="Couldn't load saved lists"
        skeleton={<p className={styles.savedEmpty}>Loading saved lists…</p>}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <p className={styles.savedEmpty}>
            No saved lists yet. Preview a filter above and save it to build your first segment.
          </p>
        }
      >
        {(rows) => (
          <ul className={styles.savedList} aria-label="Saved lists">
            {rows.map((l) => (
              <li key={l.id} className={styles.savedRow}>
                <span className={styles.savedName}>{l.name}</span>
                <span className={styles.savedMeta}>
                  {formatNumber(l.accounts)} {l.accounts === 1 ? "account" : "accounts"} · saved{" "}
                  {timeAgo(l.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </DataState>
    </Card>
  );
}
