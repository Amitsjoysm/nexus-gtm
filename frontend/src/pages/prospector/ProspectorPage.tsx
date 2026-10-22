import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Button, EmptyState, ErrorState, Icons, Skeleton, Tabs, useToast,
} from "@/components/ui";
import { AddToListModal } from "@/components/lists/AddToListModal";
import { useApiClient } from "@/app/AuthContext";
import { useApi } from "@/hooks/useApi";
import { useSelection } from "@/hooks/useSelection";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { ProspectList } from "@/lib/types";
import type {
  FilterValue, FilterValues, ProspectRow, ProspectSelection, ProspectTab, ProspectView,
} from "@/lib/prospectorTypes";
import { BulkBar } from "./BulkBar";
import type { BulkAction } from "./BulkBar";
import { ConfirmDeliveryDialog } from "./ConfirmDeliveryDialog";
import type { DeliveryRequest } from "./ConfirmDeliveryDialog";
import { FilterRail } from "./FilterRail";
import { Pager } from "./Pager";
import { ResultsTable } from "./ResultsTable";
import { SavedTable } from "./SavedTable";
import {
  activeCount, decodeFilters, encodeFilters, forRequest, inactiveHere,
} from "./filterState";
import { downloadSavedCsv } from "./savedCsv";
import { useProspectorSearch } from "./useProspectorSearch";
import styles from "./Prospector.module.css";

/** The most a single action takes (decision D17). */
const SELECT_ALL_LIMIT = 1000;
const TABS: ProspectTab[] = ["total", "net_new", "saved"];

function readView(raw: string | null): ProspectView {
  return raw === "companies" ? "companies" : "contacts";
}

function readTab(raw: string | null): ProspectTab {
  return TABS.includes(raw as ProspectTab) ? (raw as ProspectTab) : "total";
}

/**
 * Prospector: search a B2B database of people and companies the workspace does not have yet, and
 * take what is worth paying for. Browsing is free and masked; saving, listing and exporting are
 * charged per field, once (spec 2026-10-16). View, tab and filters live in the URL, so Back,
 * refresh and a shared link all land on the same search.
 */
export function ProspectorPage() {
  const api = useApiClient();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const view = readView(params.get("view"));
  const tab = readTab(params.get("tab"));
  const filtersRaw = params.get("f");
  const values = useMemo(() => decodeFilters(filtersRaw), [filtersRaw]);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [includeCount, setIncludeCount] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const [allMatching, setAllMatching] = useState(false);
  const [request, setRequest] = useState<DeliveryRequest | null>(null);
  const [listing, setListing] = useState<{ kind: "account" | "contact"; ids: string[] } | null>(
    null);
  const [verifying, setVerifying] = useState(false);

  const catalog = useApi((signal) => api.prospectorFilters(view, signal), [view]);
  const lists = useApi<ProspectList[]>((signal) => api.listSavedLists(signal), []);
  const search = useProspectorSearch({
    view, tab, catalog: catalog.data, values, page, pageSize,
    cursor: cursors[cursors.length - 1], includeCount,
  });
  const savedCount = useProspectorSearch({
    view, tab: "saved", catalog: catalog.data, values, page: 1, pageSize: 25, cursor: null,
    includeCount: false, enabled: tab !== "saved",
  });

  const state = search.data;
  const searchResult = state?.kind === "search" ? state.result : null;
  const savedResult = state?.kind === "saved" ? state.result : null;
  const visibleKeys = useMemo(() => (savedResult ? savedResult.rows.map((r) => r.id)
    : (searchResult?.rows ?? []).map((r) => r.row_key)), [savedResult, searchResult]);
  const selection = useSelection(visibleKeys);
  const { clear } = selection;

  // A different search starts again at its first page, with nothing ticked.
  useEffect(() => {
    setPage(1);
    setCursors([null]);
    setIncludeCount(false);
    setAllMatching(false);
    clear();
  }, [view, tab, filtersRaw, pageSize, clear]);

  const setUrl = useCallback((next: { view?: ProspectView; tab?: ProspectTab;
    values?: FilterValues }, replace = false) => {
    const out = new URLSearchParams(params);
    if (next.view) out.set("view", next.view);
    if (next.tab) out.set("tab", next.tab);
    if (next.values) {
      const encoded = encodeFilters(next.values);
      if (encoded) out.set("f", encoded);
      else out.delete("f");
    }
    setParams(out, { replace });
  }, [params, setParams]);

  const onFilter = useCallback((key: string, value: FilterValue) => {
    setUrl({ values: { ...values, [key]: value } }, true);
  }, [setUrl, values]);

  const savedTotal = tab === "saved" ? savedResult?.total
    : savedCount.data?.kind === "saved" ? savedCount.data.result.total : undefined;
  const totalCount = searchResult?.total ?? undefined;
  const filterCount = activeCount(catalog.data, values);
  const notHere = inactiveHere(catalog.data, values, tab);
  const isSearchTab = tab !== "saved";

  function selectionFor(rows: ProspectRow[]): ProspectSelection {
    const { filters } = forRequest(catalog.data, values, tab);
    const base = { view, scope: tab === "net_new" ? "net_new" as const : "total" as const, filters };
    if (allMatching) return { ...base, all_matching: { limit: SELECT_ALL_LIMIT } };
    return { ...base, rows: rows.map((r) => ({ api_page: r.api_page,
      api_page_size: r.api_page_size, row_key: r.row_key })) };
  }

  function deliver(action: DeliveryRequest["action"]) {
    const rows = (searchResult?.rows ?? []).filter((r) => selection.selected.has(r.row_key));
    setRequest({ action, selection: selectionFor(rows),
      count: allMatching ? SELECT_ALL_LIMIT : rows.length });
  }

  async function verifySelected() {
    setVerifying(true);
    try {
      const out = await api.prospectorVerify(selection.ids);
      if (out.mode === "queued") {
        toast.toast({ tone: "success", title: "Verifying in the background",
          description: `${formatNumber(out.count)} addresses. Statuses update as they finish.` });
      } else {
        const removed = out.results.filter((r) => !r.email_kept).length;
        const valid = out.results.filter((r) => r.status === "valid").length;
        toast.toast({ tone: "success", title: `Checked ${formatNumber(out.count)} addresses`,
          description: `${formatNumber(valid)} valid${removed ? `, ${formatNumber(removed)} removed as undeliverable` : ""}.` });
      }
      clear();
      search.refetch();
    } catch (err) {
      toast.error("Couldn't verify", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setVerifying(false);
    }
  }

  const bulkActions: BulkAction[] = isSearchTab
    ? view === "contacts"
      ? [
          { key: "save", label: "Save contacts", primary: true, icon: <Icons.PlusIcon />,
            onClick: () => deliver("save") },
          { key: "list", label: "Add to contact list", icon: <Icons.ListIcon />,
            onClick: () => deliver("list") },
          { key: "company_list", label: "Track their companies", icon: <Icons.BuildingIcon />,
            onClick: () => deliver("company_list") },
          { key: "export", label: "Export CSV", icon: <Icons.DownloadIcon />,
            onClick: () => deliver("export") },
        ]
      : [
          { key: "save", label: "Save companies", primary: true, icon: <Icons.PlusIcon />,
            onClick: () => deliver("save") },
          { key: "list", label: "Add to account list", icon: <Icons.ListIcon />,
            onClick: () => deliver("list") },
          { key: "export", label: "Export CSV", icon: <Icons.DownloadIcon />,
            onClick: () => deliver("export") },
        ]
    : [
        ...(view === "contacts" ? [{
          key: "verify", label: verifying ? "Verifying" : "Verify emails",
          icon: <Icons.ShieldCheckIcon />, onClick: () => { if (!verifying) void verifySelected(); },
        }] : []),
        { key: "list", label: view === "contacts" ? "Add to contact list" : "Add to account list",
          icon: <Icons.ListIcon />, primary: true,
          onClick: () => setListing({ kind: view === "contacts" ? "contact" : "account",
            ids: selection.ids }) },
        { key: "export", label: "Export CSV", icon: <Icons.DownloadIcon />,
          onClick: () => downloadSavedCsv(view, (savedResult?.rows ?? [])
            .filter((r) => selection.selected.has(r.id))) },
      ];

  const selectedCount = allMatching ? SELECT_ALL_LIMIT : selection.ids.length;
  const canSelectAll = isSearchTab && selection.allVisible && !!searchResult?.has_more;

  function body() {
    if (catalog.error) {
      return <ErrorState title="Couldn't load Prospector" message={catalog.error.detail}
        onRetry={catalog.refetch} />;
    }
    if (search.error) return <SearchProblem error={search.error} onRetry={search.refetch} />;
    if (!catalog.data || (search.loading && !state)) {
      return <Skeleton height={360} radius="var(--radius-lg)" />;
    }
    if (state?.kind === "idle") {
      return (
        <EmptyState
          icon={<Icons.CompassIcon />}
          title="Choose a filter to start"
          description="Search by job title, company, location, size, industry or technology. Browsing is free: emails, phones and websites stay masked until you save or export."
          action={<Button variant="secondary" className={styles.onlySmall}
            onClick={() => setRailOpen(true)}>Show filters</Button>}
        />
      );
    }
    if (searchResult && !searchResult.rows.length) {
      if (searchResult.scan_capped) {
        return (
          <EmptyState icon={<Icons.CompassIcon />}
            title={`Scanned ${formatNumber(searchResult.scanned)} records: none were new`}
            description="Your workspace already holds every match so far. Keep scanning, or narrow the filters."
            action={<Button onClick={() => setCursors((c) => [...c, searchResult.next_cursor])}>
              Keep scanning</Button>} />
        );
      }
      return <NoMatches onClear={() => setUrl({ values: {} }, true)} />;
    }
    if (savedResult && !savedResult.rows.length) {
      return (
        <EmptyState icon={<Icons.UsersIcon />}
          title={filterCount ? "Nothing you hold matches" : "No records yet"}
          description={filterCount ? "Loosen a filter, or search Total for people you don't have yet."
            : "Saved people and companies appear here."} />
      );
    }
    return (
      <>
        {savedResult ? (
          <SavedTable view={view} rows={savedResult.rows} loading={search.loading}
            selected={selection.selected} allVisible={selection.allVisible}
            someVisible={selection.someVisible} onToggle={selection.toggle}
            onToggleAll={selection.toggleVisible} />
        ) : searchResult ? (
          <ResultsTable view={view} rows={searchResult.rows} loading={search.loading}
            selected={selection.selected} allVisible={selection.allVisible}
            someVisible={selection.someVisible} onToggle={selection.toggle}
            onToggleAll={() => { setAllMatching(false); selection.toggleVisible(); }}
            onReveal={(row) => setRequest({ action: "reveal", count: 1,
              selection: { ...selectionFor([row]), rows: [{ api_page: row.api_page,
                api_page_size: row.api_page_size, row_key: row.row_key }],
                all_matching: undefined } })} />
        ) : null}
        {pager()}
      </>
    );
  }

  function pager() {
    if (savedResult) {
      const pages = Math.max(1, Math.ceil(savedResult.total / pageSize));
      return <Pager position={`Page ${page} of ${formatNumber(pages)}`} canPrev={page > 1}
        canNext={page < pages} onPrev={() => setPage((p) => p - 1)}
        onNext={() => setPage((p) => p + 1)} pageSize={pageSize} onPageSize={setPageSize} />;
    }
    if (!searchResult) return null;
    if (tab === "net_new") {
      return <Pager
        position={`Scanned ${formatNumber(searchResult.scanned)} · batch ${cursors.length}`}
        canPrev={cursors.length > 1} canNext={!!searchResult.next_cursor}
        nextLabel={searchResult.scan_capped ? "Keep scanning" : "Next"}
        onPrev={() => setCursors((c) => c.slice(0, -1))}
        onNext={() => setCursors((c) => [...c, searchResult.next_cursor])}
        pageSize={pageSize} onPageSize={setPageSize} />;
    }
    const pages = searchResult.total !== null ? Math.ceil(searchResult.total / pageSize) : null;
    return <Pager position={pages ? `Page ${page} of ${formatNumber(pages)}` : `Page ${page}`}
      canPrev={page > 1} canNext={searchResult.has_more} onPrev={() => setPage((p) => p - 1)}
      onNext={() => setPage((p) => p + 1)} pageSize={pageSize} onPageSize={setPageSize} />;
  }

  const shown = savedResult?.rows.length ?? searchResult?.rows.length;

  return (
    <div className={styles.page}>
      <PageHeader
        title="Prospector"
        description="Find people and companies you don't have yet. Browsing is free; emails, phones and websites stay masked until you save or export."
        actions={
          <Tabs variant="segmented" idPrefix="prospector-view" value={view}
            onChange={(v) => setUrl({ view: readView(v) })}
            items={[{ value: "contacts", label: "Contacts" }, { value: "companies", label: "Companies" }]} />
        }
      />

      <div className={styles.layout}>
        <div className={railOpen ? styles.railSlot : `${styles.railSlot} ${styles.railClosed}`}>
          {catalog.data ? (
            <FilterRail key={view} catalog={catalog.data} values={values} tab={tab}
              lists={lists.data ?? []} onChange={onFilter}
              onClearAll={() => setUrl({ values: {} }, true)}
              onClearKeys={(keys) => {
                const next = { ...values };
                for (const k of keys) delete next[k];
                setUrl({ values: next }, true);
              }} />
          ) : (
            <Skeleton height={480} radius="var(--radius-lg)" />
          )}
        </div>

        <section className={styles.main} aria-label="Results">
          <div className={styles.toolbar}>
            <Tabs idPrefix="prospector-tab" value={tab} onChange={(t) => setUrl({ tab: readTab(t) })}
              items={[
                { value: "total", label: "Total", count: totalCount ?? undefined },
                { value: "net_new", label: "Net new" },
                { value: "saved", label: "Saved", count: savedTotal },
              ]} />
            <div className={styles.toolbarEnd}>
              <Button variant="secondary" size="sm" className={styles.onlySmall}
                aria-expanded={railOpen} onClick={() => setRailOpen((o) => !o)}
                iconLeft={<Icons.SearchIcon />}>
                Filters{filterCount ? ` (${filterCount})` : ""}
              </Button>
              {/* Only where the provider can count: it cannot today, and the button timed out on
                  contacts and re-bought a page for nothing on companies (2026-10-22). */}
              {tab === "total" && catalog.data?.can_count && searchResult
                && searchResult.total === null && (
                <Button variant="ghost" size="sm" onClick={() => setIncludeCount(true)}>
                  Show total
                </Button>
              )}
            </div>
          </div>

          {notHere.length > 0 && (
            <p className={styles.notice}>
              <Icons.LockIcon />
              Not applied on this tab: {notHere.join(", ")}.
            </p>
          )}

          {selectedCount > 0 && (
            <BulkBar count={selectedCount} allMatching={allMatching} canSelectAll={canSelectAll}
              selectAllLimit={SELECT_ALL_LIMIT} onSelectAll={() => setAllMatching(true)}
              onClear={() => { setAllMatching(false); clear(); }} actions={bulkActions} />
          )}

          {body()}

          <p className="sr-only" aria-live="polite">
            {shown !== undefined ? `${shown} results shown, page ${page}.` : ""}
          </p>
        </section>
      </div>

      <ConfirmDeliveryDialog request={request} onClose={() => setRequest(null)}
        onDone={() => { setAllMatching(false); clear(); search.refetch(); savedCount.refetch();
          lists.refetch(); }} />
      {listing && (
        <AddToListModal open onClose={() => setListing(null)} kind={listing.kind}
          accountIds={listing.kind === "account" ? listing.ids : []}
          contactIds={listing.kind === "contact" ? listing.ids : []}
          onDone={() => { clear(); lists.refetch(); }} />
      )}
    </div>
  );
}

function NoMatches({ onClear }: { onClear: () => void }) {
  return (
    <EmptyState icon={<Icons.SearchIcon />} title="No matches"
      description="Loosen a filter or clear them to see more."
      action={<Button variant="secondary" onClick={onClear}>Clear filters</Button>} />
  );
}

/** Each way a search can be refused is its own sentence and, where useful, its own next step. */
function SearchProblem({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  if (error.status === 400 && /not set up/i.test(error.detail)) {
    return <EmptyState icon={<Icons.PlugIcon />} title="Prospector is not set up yet"
      description={error.detail} />;
  }
  if (error.status === 429) {
    return <EmptyState icon={<Icons.ClockIcon />}
      title={/limit/i.test(error.detail) ? "Today's browsing limit is used up" : "Slow down a little"}
      description={error.detail} />;
  }
  if (error.status === 402) {
    return <EmptyState icon={<Icons.LockIcon />} title="Prospector is not available"
      description={error.switchMessage || error.detail} />;
  }
  if (error.status === 422) {
    return <EmptyState icon={<Icons.AlertTriangleIcon />} title="That search can't run"
      description={error.detail} />;
  }
  return <ErrorState title="The data provider didn't answer" message={error.detail}
    onRetry={onRetry} />;
}
