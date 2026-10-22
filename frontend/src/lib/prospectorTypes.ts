/**
 * Prospector's wire types (`/api/prospector/*`, `nexus/prospector/schemas.py`).
 *
 * A search row carries NO raw email, phone, domain or LinkedIn field, by design (spec §5): only the
 * masks, the `has_*` flags and business-card facts. `record` is filled only for a row the workspace
 * already holds, from the workspace's own data. `tests/test_prospector_ui.py` pins that this type
 * never grows a raw PII field.
 */

export type ProspectView = "contacts" | "companies";
export type ProspectScope = "total" | "net_new";
export type ProspectTab = ProspectScope | "saved";

export type FieldKind = "text" | "text_list" | "enum_list" | "bool" | "id_list" | "window";

export interface FilterFieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  options: string[];
  /** Searchable at the provider (Total / Net new). */
  api: boolean;
  /** Applies on the Saved tab. */
  saved: boolean;
}

export interface FilterSectionSpec {
  key: string;
  label: string;
  hint: string;
  workspace_only: boolean;
  fields: FilterFieldSpec[];
}

export interface FilterCatalog {
  view: ProspectView;
  sections: FilterSectionSpec[];
  /** Whether the data provider can count matches for this view ("Show total"). */
  can_count?: boolean;
}

export interface SavedRecordRef {
  kind: "contact" | "account";
  id: string;
  account_id: string | null;
  email: string | null;
  phone: string | null;
  domain: string | null;
}

export interface ProspectRow {
  row_key: string;
  api_page: number;
  api_page_size: number;
  saved: boolean;
  record: SavedRecordRef | null;
  first_name: string;
  last_name: string;
  title: string;
  department: string;
  seniority: string;
  company_name: string;
  country: string;
  state: string;
  city: string;
  industry: string;
  employee_size_range: string;
  email_masked: string;
  phone_masked: string;
  domain_masked: string;
  linkedin_masked: string;
  has_email: boolean;
  has_direct_phone: boolean;
  has_linkedin: boolean;
}

export interface ProspectSearchResult {
  rows: ProspectRow[];
  page: number;
  page_size: number;
  has_more: boolean;
  total: number | null;
  next_cursor: string | null;
  scanned: number;
  exhausted: boolean;
  scan_capped: boolean;
}

export interface SavedRow {
  kind: "contact" | "account";
  id: string;
  account_id: string;
  name: string;
  title: string;
  seniority: string;
  email: string | null;
  email_status: string | null;
  phone: string | null;
  company_name: string;
  domain: string | null;
  country: string | null;
  region: string | null;
  city: string | null;
  industry: string | null;
  employee_count: number | null;
  enrichment_source: string | null;
}

export interface SavedResult {
  rows: SavedRow[];
  page: number;
  page_size: number;
  total: number;
}

/** Filter values as the rail holds them, keyed by field key. */
export type FilterValue = string | string[] | boolean | number | null;
export type FilterValues = Record<string, FilterValue>;

export interface RowRef {
  api_page: number;
  api_page_size: number;
  row_key: string;
}

export interface ProspectSelection {
  view: ProspectView;
  scope: ProspectScope;
  filters: FilterValues;
  rows?: RowRef[];
  all_matching?: { limit: number };
}

export type DeliverAction = "save" | "list" | "reveal" | "company_list";
export type QuoteAction = DeliverAction | "export";

export interface ProspectQuote {
  records: number;
  charges: Record<string, number>;
  credits: Record<string, number>;
  total_credits: number;
  already_paid: number;
  held: number;
  unavailable: number;
  balance: number | null;
  enough: boolean;
}

export interface ProspectDeliverResult {
  created: number;
  reused: number;
  listed: number;
  held: number;
  unavailable: number;
  already_paid: number;
  dropped_linkedin: number;
  charged: Record<string, number>;
  record_ids: string[];
  list_id: string | null;
}

export interface ProspectVerifyResult {
  mode: "inline" | "queued";
  count: number;
  charged: number;
  results: { contact_id: string; status: string; email_kept: boolean }[];
}
