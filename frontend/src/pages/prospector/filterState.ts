/**
 * Prospector's filter state: what the rail holds, what each tab may send, and the URL form.
 *
 * The rail keeps one flat `{fieldKey: value}` map for both views. Which of those values a request
 * carries is decided here from the server's catalog (`GET /prospector/filters`), never from a list
 * kept in the client: the provider tabs send only fields the provider can search, and the Saved tab
 * sends only fields workspace records store, with the NEXUS-only ones as `workspace`.
 */
import type {
  FilterCatalog,
  FilterFieldSpec,
  FilterSectionSpec,
  FilterValue,
  FilterValues,
  ProspectTab,
} from "@/lib/prospectorTypes";

/** A modifier of another filter (the signal window), never a filter on its own. */
const MODIFIERS = new Set(["signal_days"]);
export const MAX_VALUES = 50;
export const MAX_CHARS = 120;

export function isFilled(value: FilterValue | undefined): boolean {
  if (value === null || value === undefined || value === false) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "string") return value.trim() !== "";
  return true;
}

export function clean(values: FilterValues): FilterValues {
  const out: FilterValues = {};
  for (const [key, value] of Object.entries(values)) {
    if (isFilled(value)) out[key] = value;
  }
  return out;
}

/** Whether a field can apply on this tab (an unusable one is shown disabled with the reason). */
export function usableOn(
  field: FilterFieldSpec,
  section: FilterSectionSpec,
  tab: ProspectTab,
): boolean {
  if (tab === "saved") return field.saved;
  return field.api && !section.workspace_only;
}

export function sectionCount(section: FilterSectionSpec, values: FilterValues): number {
  return section.fields.filter((f) => !MODIFIERS.has(f.key) && isFilled(values[f.key])).length;
}

export function activeCount(catalog: FilterCatalog | null, values: FilterValues): number {
  if (!catalog) return 0;
  return catalog.sections.reduce((n, s) => n + sectionCount(s, values), 0);
}

/**
 * What a request for this tab carries. Values for fields that cannot apply here are left out (the
 * rail shows them disabled), so switching tabs never sends a filter the server would refuse.
 */
export function forRequest(
  catalog: FilterCatalog | null,
  values: FilterValues,
  tab: ProspectTab,
): { filters: FilterValues; workspace: FilterValues } {
  const filters: FilterValues = {};
  const workspace: FilterValues = {};
  if (!catalog) return { filters, workspace };
  for (const section of catalog.sections) {
    for (const field of section.fields) {
      const value = values[field.key];
      if (!isFilled(value) || !usableOn(field, section, tab)) continue;
      if (section.workspace_only) workspace[field.key] = value;
      else filters[field.key] = value;
    }
  }
  // The window only means something beside a signal filter.
  if (tab === "saved" && Object.keys(workspace).some((k) => k !== "signal_days")) {
    const days = values.signal_days;
    if (typeof days === "number") workspace.signal_days = days;
  } else {
    delete workspace.signal_days;
  }
  return { filters, workspace };
}

/** Values that cannot apply on this tab, by section label: shown so nothing is silently dropped. */
export function inactiveHere(
  catalog: FilterCatalog | null,
  values: FilterValues,
  tab: ProspectTab,
): string[] {
  if (!catalog) return [];
  const labels: string[] = [];
  for (const section of catalog.sections) {
    const unusable = section.fields.some(
      (f) => !MODIFIERS.has(f.key) && isFilled(values[f.key]) && !usableOn(f, section, tab),
    );
    if (unusable) labels.push(section.label);
  }
  return labels;
}

// ---- the URL (`?view=&tab=&f=`), so Back, refresh and a shared link keep the search ----------

export function encodeFilters(values: FilterValues): string {
  const cleaned = clean(values);
  return Object.keys(cleaned).length ? JSON.stringify(cleaned) : "";
}

export function decodeFilters(raw: string | null): FilterValues {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: FilterValues = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!/^[a-z_]{1,40}$/.test(key)) continue;
      if (typeof value === "string") out[key] = value.slice(0, MAX_CHARS);
      else if (typeof value === "boolean" || typeof value === "number") out[key] = value;
      else if (Array.isArray(value)) {
        out[key] = value.filter((v): v is string => typeof v === "string")
          .slice(0, MAX_VALUES).map((v) => v.slice(0, MAX_CHARS));
      }
    }
    return out;
  } catch {
    return {};
  }
}
