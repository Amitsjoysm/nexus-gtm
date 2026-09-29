import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Icons,
  Input,
  ScoreMeter,
  Select,
  Spinner,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { formatNumber } from "@/lib/format";
import type {
  CustomFieldEntity,
  DiscoveryCandidate,
  DiscoveryResult,
  ResultsQuery,
  Role,
} from "@/lib/types";
import { AddToListModal } from "@/components/lists/AddToListModal";
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
 * dynamic per-tenant custom-field columns, multi-select → a list or a campaign, and (admin+) a CSV
 * import of proprietary data. Reads `GET /orchestration/runs/{id}/results`.
 *
 * Discovery SAVES what it finds as it runs: new companies land in Accounts and new people in
 * Contacts. The panel says so and links there, because a results table with no word about that
 * reads as a preview that still needs an "Add" click. The per-row Research button that used to sit
 * here started a research-draft-send run through the old sending path; it is gone, and the
 * account page is where research and drafting happen.
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
  const [listModalOpen, setListModalOpen] = useState(false);

  const candidates = data?.candidates ?? [];
  const dynamicColumns = data?.columns ?? [];

  // Custom fields attach to the entity these results describe — accounts unless the run targets
  // people. Prefer the candidates' own entity; fall back to the run's target label.
  const importEntity: CustomFieldEntity =
    candidates[0]?.entity ??
    (/contact|people|person|lead/i.test(data?.target ?? "") ? "contact" : "account");

  function toggleRow(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Every row is a saved record (discovery persists what it finds), so every row can go on a list
  // or into a campaign.
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
            label="Select everyone on this page"
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
            {c.entity === "account" ? (
              <Link to={`/accounts/${c.id}`} className={styles.nameLink}>
                {c.name}
              </Link>
            ) : (
              <span className={styles.name}>{c.name}</span>
            )}
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
        width: "124px",
        // "Added" says what happened: this run created the record. "In CRM" was wrong for anything
        // typed in by hand or imported from a file, which is most of what a workspace holds.
        render: (c) =>
          c.is_new || c.source === "discovery" ? (
            <Badge tone="info">Added</Badge>
          ) : (
            <Badge tone="neutral">Already yours</Badge>
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

    return cols;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    dynamicColumns,
    selected,
    allPageSelected,
    somePageSelected,
    selectablePageIds,
  ]);

  const total = data?.total ?? 0;
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(offset + PAGE_SIZE, total);
  const counts = data?.counts ?? {};
  const added = counts.new ?? 0;
  const already = counts.own ?? 0;
  const people = importEntity === "contact";
  const addedWho = people
    ? added === 1 ? "person was" : "people were"
    : added === 1 ? "company was" : "companies were";

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
                { value: "own", label: "Already yours" },
                { value: "new", label: "Added by this run" },
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

      {added > 0 && (
        <div className={styles.addedNotice} role="status">
          <Icons.CheckIcon />
          <span>
            {formatNumber(added)} new {addedWho} added to your {people ? "Contacts" : "Accounts"}.
          </span>
          <Link to={people ? "/contacts" : "/accounts?source=found"} className={styles.addedLink}>
            {people ? "View in Contacts" : "View in Accounts"}
          </Link>
        </div>
      )}

      <div className={styles.summary}>
        <span>
          {total === 0 ? "No matches" : `${start}–${end} of ${total}`}
        </span>
        {already > 0 && <span className={styles.count}>{formatNumber(already)} already yours</span>}
        {added > 0 && <span className={styles.count}>{formatNumber(added)} added by this run</span>}
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
              variant="secondary"
              iconLeft={<Icons.ListIcon />}
              onClick={() => setListModalOpen(true)}
            >
              Add to list
            </Button>
            {canRun && (
              <Button
                size="sm"
                iconLeft={<Icons.SendIcon />}
                onClick={() => navigate("/engagement/campaigns/new", {
                  state: selectionForCampaign(candidates, selected),
                })}
              >
                Add to a campaign
              </Button>
            )}
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

      <AddToListModal
        open={listModalOpen}
        kind={people ? "contact" : "account"}
        contactIds={people ? selectionForCampaign(candidates, selected).contactIds : []}
        accountIds={people ? [] : selectionForCampaign(candidates, selected).accountIds}
        onClose={() => setListModalOpen(false)}
        onDone={() => setSelected(new Set())}
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
  return !!c.id;
}

/* ---------------------------- add to a campaign --------------------------- */

/**
 * The selection, split the way a campaign takes it: people by id, and companies, which the server
 * turns into everyone there with an email address. The campaign builder receives it in router
 * state and adds them once the campaign exists; nothing is drafted or sent until the SDR reviews.
 */
export function selectionForCampaign(candidates: DiscoveryCandidate[], selected: Set<string>) {
  const contactIds: string[] = [];
  const accountIds: string[] = [];
  for (const cand of candidates) {
    if (!selected.has(cand.id)) continue;
    if (cand.entity === "contact") contactIds.push(cand.id);
    else accountIds.push(cand.id);
  }
  return { contactIds, accountIds };
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
