import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Icons,
  Input,
  Modal,
  ScoreMeter,
  Select,
  Spinner,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { humanize } from "@/lib/format";
import type {
  CustomFieldEntity,
  DiscoveryCandidate,
  DiscoveryResult,
  ResultsQuery,
  Role,
} from "@/lib/types";
import { ImportCsvModal } from "./ImportCsvModal";
import styles from "./ResultsPanel.module.css";

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };
const PAGE_SIZE = 25;

type SourceFilter = "all" | "own" | "new";

export interface ResultsPanelProps {
  runId: string;
}

/**
 * Discovery results table for a run: server-side filtered/paginated candidates with a fit meter,
 * dynamic per-tenant custom-field columns, a per-row Research action, multi-select → save the
 * matching accounts as a Relevance list, and (admin+) a CSV import of proprietary data.
 * Reads `GET /orchestration/runs/{id}/results`.
 */
export function ResultsPanel({ runId }: ResultsPanelProps) {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const { session } = useAuth();
  const canRun = session ? ROLE_RANK[session.role] >= ROLE_RANK.manager : false;
  const canImport = session ? ROLE_RANK[session.role] >= ROLE_RANK.admin : false;
  const [importOpen, setImportOpen] = useState(false);

  // Filters. Selects apply immediately; free-text is debounced so typing doesn't thrash the server.
  const [source, setSource] = useState<SourceFilter>("all");
  const [minFit, setMinFit] = useState("");
  const [text, setText] = useState<{ q: string; cf: Record<string, string> }>({ q: "", cf: {} });
  const debounced = useDebounced(text, 300);
  const [showFieldFilters, setShowFieldFilters] = useState(false);
  const [offset, setOffset] = useState(0);

  // Reset to the first page whenever the filter criteria change (not on page navigation).
  useEffect(() => {
    setOffset(0);
  }, [source, minFit, debounced]);

  const query: ResultsQuery = useMemo(() => {
    const q: ResultsQuery = { limit: PAGE_SIZE, offset };
    if (source !== "all") q.source = source === "new" ? "discovery" : "own";
    const fit = Number(minFit);
    if (minFit.trim() !== "" && !Number.isNaN(fit)) q.min_fit = fit;
    if (debounced.q.trim() !== "") q.q = debounced.q.trim();
    for (const [key, value] of Object.entries(debounced.cf)) {
      if (value.trim() !== "") q[`cf_${key}`] = value.trim();
    }
    return q;
  }, [source, minFit, debounced, offset]);

  const queryKey = JSON.stringify(query);
  const results = useApi<DiscoveryResult>(
    (signal) => api.getRunResults(runId, query, signal),
    [runId, queryKey],
  );
  const data = results.data;
  const refreshing = results.loading && !!data;

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [researchingId, setResearchingId] = useState<string | null>(null);
  const [listModalOpen, setListModalOpen] = useState(false);

  const candidates = data?.candidates ?? [];
  const dynamicColumns = data?.columns ?? [];

  // Custom fields attach to the entity these results describe — accounts unless the run targets
  // people. Prefer the candidates' own entity; fall back to the run's target label.
  const importEntity: CustomFieldEntity =
    candidates[0]?.entity ??
    (/contact|people|person|lead/i.test(data?.target ?? "") ? "contact" : "account");

  async function research(candidate: DiscoveryCandidate) {
    setResearchingId(candidate.id);
    try {
      const run = await api.createRun({ goal: "research_account", account_id: candidate.id });
      navigate(`/runs/${run.id}`);
    } catch (err) {
      toast.error(
        "Couldn't start research",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
      setResearchingId(null);
    }
  }

  function toggleRow(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Selection drives a Relevance list (the backend builds lists from a fit floor, not arbitrary
  // ids), so only persisted accounts are selectable.
  const selectablePageIds = useMemo(
    () => candidates.filter(isSelectable).map((c) => c.id),
    [candidates],
  );
  const allPageSelected =
    selectablePageIds.length > 0 && selectablePageIds.every((id) => selected.has(id));
  const somePageSelected = selectablePageIds.some((id) => selected.has(id));

  function togglePage() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allPageSelected) for (const id of selectablePageIds) next.delete(id);
      else for (const id of selectablePageIds) next.add(id);
      return next;
    });
  }

  const columns = useMemo<Column<DiscoveryCandidate>[]>(() => {
    const cols: Column<DiscoveryCandidate>[] = [
      {
        key: "select",
        width: "36px",
        align: "center",
        header: (
          <CheckBox
            checked={allPageSelected}
            indeterminate={!allPageSelected && somePageSelected}
            disabled={selectablePageIds.length === 0}
            onChange={togglePage}
            label="Select all accounts on this page"
          />
        ),
        render: (c) =>
          isSelectable(c) ? (
            <CheckBox
              checked={selected.has(c.id)}
              onChange={() => toggleRow(c.id)}
              label={`Select ${c.name}`}
            />
          ) : null,
      },
      {
        key: "name",
        header: "Name",
        render: (c) => (
          <div className={styles.nameCell}>
            <span className={styles.name}>{c.name}</span>
            {c.industry && <span className={styles.sub}>{c.industry}</span>}
          </div>
        ),
      },
      {
        key: "detail",
        header: "Domain / Email",
        hideOnMobile: true,
        render: (c) =>
          c.entity === "account" ? (
            c.domain ? (
              <span className={styles.mono}>{c.domain}</span>
            ) : (
              <span className={styles.muted}>—</span>
            )
          ) : (
            <div className={styles.nameCell}>
              <span className={styles.mono}>{c.email || "—"}</span>
              {c.title && <span className={styles.sub}>{c.title}</span>}
            </div>
          ),
      },
      {
        key: "fit",
        header: "Fit",
        width: "160px",
        render: (c) => (
          <div className={styles.fitCell}>
            <ScoreMeter value={c.fit_score} />
            <span className={styles.fitValue}>{Math.round(c.fit_score)}</span>
          </div>
        ),
      },
      {
        key: "reasons",
        header: "Why",
        hideOnMobile: true,
        render: (c) => <Reasons reasons={c.fit_reasons} />,
      },
      {
        key: "source",
        header: "Source",
        width: "92px",
        render: (c) =>
          c.is_new || c.source === "discovery" ? (
            <Badge tone="info">New</Badge>
          ) : (
            <Badge tone="neutral">In CRM</Badge>
          ),
      },
    ];

    for (const col of dynamicColumns) {
      cols.push({
        key: `cf_${col.key}`,
        header: col.label,
        hideOnMobile: true,
        render: (c) => <CustomValue value={c.custom_fields?.[col.key]} />,
      });
    }

    if (canRun) {
      cols.push({
        key: "actions",
        header: "",
        width: "120px",
        align: "right",
        render: (c) =>
          c.entity === "account" ? (
            <Button
              size="sm"
              variant="secondary"
              iconLeft={<Icons.SparklesIcon />}
              loading={researchingId === c.id}
              disabled={researchingId !== null && researchingId !== c.id}
              onClick={() => research(c)}
            >
              Research
            </Button>
          ) : null,
      });
    }

    return cols;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    dynamicColumns,
    selected,
    allPageSelected,
    somePageSelected,
    selectablePageIds,
    researchingId,
    canRun,
  ]);

  const total = data?.total ?? 0;
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(offset + PAGE_SIZE, total);
  const counts = data?.counts ?? {};

  return (
    <section className={styles.panel} aria-label="Discovery results">
      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <Field label="Search">
            <Input
              value={text.q}
              onChange={(e) => setText((t) => ({ ...t, q: e.target.value }))}
              placeholder="Name, domain, or email"
              iconLeft={<Icons.SearchIcon />}
              aria-label="Search results"
            />
          </Field>
          <Field label="Source">
            <Select
              value={source}
              onChange={(e) => setSource(e.target.value as SourceFilter)}
              options={[
                { value: "all", label: "All sources" },
                { value: "own", label: "In your CRM" },
                { value: "new", label: "Newly discovered" },
              ]}
              aria-label="Filter by source"
            />
          </Field>
          <Field label="Min fit">
            <Input
              type="number"
              min={0}
              max={100}
              value={minFit}
              onChange={(e) => setMinFit(e.target.value)}
              placeholder="0"
              className={styles.fitInput}
              aria-label="Minimum fit score"
            />
          </Field>
        </div>

        <div className={styles.toolbarRight}>
          {refreshing && (
            <span className={styles.refreshing} role="status">
              <Spinner size={14} /> Updating…
            </span>
          )}
          {dynamicColumns.length > 0 && (
            <Button
              size="sm"
              variant="ghost"
              aria-expanded={showFieldFilters}
              onClick={() => setShowFieldFilters((v) => !v)}
            >
              Field filters
            </Button>
          )}
          {canImport && (
            <Button
              size="sm"
              variant="secondary"
              iconLeft={<Icons.PlusIcon />}
              onClick={() => setImportOpen(true)}
            >
              Import data
            </Button>
          )}
        </div>
      </div>

      {showFieldFilters && dynamicColumns.length > 0 && (
        <div className={styles.fieldFilters}>
          {dynamicColumns.map((col) => (
            <Field key={col.key} label={col.label}>
              <Input
                value={text.cf[col.key] ?? ""}
                onChange={(e) =>
                  setText((t) => ({ ...t, cf: { ...t.cf, [col.key]: e.target.value } }))
                }
                placeholder={`Filter ${col.label.toLowerCase()}`}
                aria-label={`Filter by ${col.label}`}
              />
            </Field>
          ))}
        </div>
      )}

      <div className={styles.summary}>
        <span>
          {total === 0 ? "No matches" : `${start}–${end} of ${total}`}
        </span>
        {Object.entries(counts).map(([key, n]) => (
          <span key={key} className={styles.count}>
            {humanize(key)}: {n}
          </span>
        ))}
      </div>

      {selected.size > 0 && (
        <div className={styles.selectionBar} role="region" aria-label="Selection actions">
          <span className={styles.selectionCount}>{selected.size} selected</span>
          <div className={styles.selectionActions}>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setSelected(new Set())}
            >
              Clear
            </Button>
            <Button
              size="sm"
              iconLeft={<Icons.ListIcon />}
              onClick={() => setListModalOpen(true)}
            >
              Add to list
            </Button>
          </div>
        </div>
      )}

      {results.error && !data ? (
        <ErrorState
          title="Couldn't load results"
          message={results.error.detail}
          onRetry={results.refetch}
        />
      ) : (
        <DataTable
          columns={columns}
          rows={candidates}
          getRowKey={(c) => `${c.entity}:${c.id}`}
          loading={results.loading && !data}
          skeletonRows={6}
          caption="Discovery results"
          // Hold the table's shape when the chat dock narrows the console; scroll instead of crush.
          // Base columns need ~720px; each dynamic custom-field column adds ~150px.
          minWidth={720 + dynamicColumns.length * 150}
          empty={
            <EmptyState
              compact
              icon={<Icons.TargetIcon />}
              title="No matches yet"
              description="Refine the ICP in chat, or loosen the filters above."
            />
          }
        />
      )}

      <div className={styles.pager}>
        <Button
          size="sm"
          variant="secondary"
          iconLeft={<Icons.ChevronLeftIcon />}
          disabled={offset === 0 || results.loading}
          onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
        >
          Previous
        </Button>
        <Button
          size="sm"
          variant="secondary"
          iconRight={<Icons.ChevronRightIcon />}
          disabled={end >= total || results.loading}
          onClick={() => setOffset((o) => o + PAGE_SIZE)}
        >
          Next
        </Button>
      </div>

      <SaveListModal
        open={listModalOpen}
        candidates={candidates}
        selected={selected}
        onClose={() => setListModalOpen(false)}
        onSaved={() => {
          setListModalOpen(false);
          setSelected(new Set());
        }}
      />

      {canImport && (
        <ImportCsvModal
          open={importOpen}
          entity={importEntity}
          onClose={() => setImportOpen(false)}
          onImported={(res) => {
            toast.success(
              "Import complete",
              `${res.updated} record${res.updated === 1 ? "" : "s"} updated` +
                (res.created_fields.length > 0
                  ? `, ${res.created_fields.length} new column${
                      res.created_fields.length === 1 ? "" : "s"
                    }.`
                  : "."),
            );
            void results.refetch();
          }}
        />
      )}
    </section>
  );
}

function isSelectable(c: DiscoveryCandidate): boolean {
  return c.entity === "account" && !c.is_new;
}

/* ------------------------------- save list ------------------------------- */

function SaveListModal({
  open,
  candidates,
  selected,
  onClose,
  onSaved,
}: {
  open: boolean;
  candidates: DiscoveryCandidate[];
  selected: Set<string>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  // The lowest fit among the selected accounts becomes the list's fit floor.
  const floor = useMemo(() => {
    const fits = candidates.filter((c) => selected.has(c.id)).map((c) => Math.round(c.fit_score));
    return fits.length > 0 ? Math.min(...fits) : 0;
  }, [candidates, selected]);

  async function save() {
    const trimmed = name.trim();
    if (trimmed === "") return;
    setBusy(true);
    try {
      const res = await api.buildList(trimmed, { min_composite: floor });
      toast.success(
        `Saved “${res.name}”`,
        `${res.accounts} account${res.accounts === 1 ? "" : "s"} scoring ${floor}+ are in this list.`,
      );
      setName("");
      onSaved();
    } catch (err) {
      toast.error(
        "Couldn't save the list",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Save as list"
      description={`Builds a Relevance list of every account scoring ${floor} or higher.`}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={name.trim() === ""}>
            Create list
          </Button>
        </>
      }
    >
      <Field label="List name">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. High-fit fintech"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void save();
            }
          }}
        />
      </Field>
    </Modal>
  );
}

/* -------------------------------- cells ---------------------------------- */

function Reasons({ reasons }: { reasons: string[] }) {
  if (!reasons || reasons.length === 0) return <span className={styles.muted}>—</span>;
  const [first, ...rest] = reasons;
  return (
    <span className={styles.reasons} title={reasons.join("\n")}>
      <span className={styles.reasonText}>{first}</span>
      {rest.length > 0 && <span className={styles.reasonMore}>+{rest.length}</span>}
    </span>
  );
}

function CustomValue({ value }: { value: unknown }): ReactNode {
  if (value === null || value === undefined || value === "") {
    return <span className={styles.muted}>—</span>;
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return <span className={styles.mono}>{JSON.stringify(value)}</span>;
  return String(value);
}

function CheckBox({
  checked,
  indeterminate,
  disabled,
  onChange,
  label,
}: {
  checked: boolean;
  indeterminate?: boolean;
  disabled?: boolean;
  onChange: () => void;
  label: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = !!indeterminate && !checked;
  }, [indeterminate, checked]);
  return (
    <input
      ref={ref}
      type="checkbox"
      className={styles.checkbox}
      checked={checked}
      disabled={disabled}
      onChange={onChange}
      aria-label={label}
    />
  );
}

/* -------------------------------- hooks ---------------------------------- */

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}
