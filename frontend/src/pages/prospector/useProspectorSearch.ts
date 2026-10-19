import { useEffect, useState } from "react";
import { useApiClient } from "@/app/AuthContext";
import { useApi } from "@/hooks/useApi";
import type {
  FilterCatalog,
  FilterValues,
  ProspectSearchResult,
  ProspectTab,
  ProspectView,
  SavedResult,
} from "@/lib/prospectorTypes";
import { forRequest } from "./filterState";

/** Filters apply after a pause in typing; paging, tabs and views apply at once. */
export const FILTER_DEBOUNCE_MS = 800;

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

export interface SearchParams {
  view: ProspectView;
  tab: ProspectTab;
  catalog: FilterCatalog | null;
  values: FilterValues;
  page: number;
  pageSize: number;
  cursor: string | null;
  includeCount: boolean;
  /** Off: answer idle without asking (the Saved count while the Saved tab itself is open). */
  enabled?: boolean;
}

export type SearchState =
  | { kind: "idle" }
  | { kind: "search"; result: ProspectSearchResult }
  | { kind: "saved"; result: SavedResult };

/**
 * The current tab's results. Total and Net new ask the provider and wait for at least one filter:
 * an unfiltered page still buys its rows, and a rep looking at the empty state has not asked for
 * anything yet. Saved reads the workspace and loads straight away. A stale request is aborted when
 * the next one starts (`useApi`).
 */
export function useProspectorSearch(params: SearchParams) {
  const api = useApiClient();
  const { filters, workspace } = forRequest(params.catalog, params.values, params.tab);
  const filtersKey = useDebounced(JSON.stringify({ filters, workspace }), FILTER_DEBOUNCE_MS);
  const ready = params.catalog !== null && params.enabled !== false;

  return useApi<SearchState>(
    async (signal) => {
      if (!ready) return { kind: "idle" };
      const settled = JSON.parse(filtersKey) as { filters: FilterValues; workspace: FilterValues };
      if (params.tab === "saved") {
        const result = await api.prospectorSaved({
          view: params.view, filters: settled.filters, workspace: settled.workspace,
          page: params.page, page_size: params.pageSize,
        }, signal);
        return { kind: "saved", result };
      }
      if (!Object.keys(settled.filters).length) return { kind: "idle" };
      const result = await api.prospectorSearch({
        view: params.view, scope: params.tab, filters: settled.filters,
        page: params.page, page_size: params.pageSize,
        cursor: params.tab === "net_new" ? params.cursor : null,
        include_count: params.tab === "total" ? params.includeCount : false,
      }, signal);
      return { kind: "search", result };
    },
    [ready, params.view, params.tab, filtersKey, params.page, params.pageSize, params.cursor,
      params.includeCount],
  );
}
