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
 *
 * `values` are the SEARCHED filters (the URL), never the rail's draft: a search runs when the rail's
 * Search applies them, so nothing here waits for typing to settle. It used to fire 800 ms after the
 * last change, buying a page for every half-built combination of filters on the way.
 */
export function useProspectorSearch(params: SearchParams) {
  const api = useApiClient();
  const { filters, workspace } = forRequest(params.catalog, params.values, params.tab);
  const filtersKey = JSON.stringify({ filters, workspace });
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
