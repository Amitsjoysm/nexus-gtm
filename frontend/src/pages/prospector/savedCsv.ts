/**
 * Export selected Saved-tab rows as CSV, in the browser.
 *
 * These are the workspace's own records, already on screen, so exporting them spends nothing and
 * needs no server round trip. Cells that would start a spreadsheet formula are neutralised exactly
 * as the server's `csv_export.safe_cell` does, so a value from any source opens as text.
 */
import type { ProspectView, SavedRow } from "@/lib/prospectorTypes";

const FORMULA_LEADS = ["=", "+", "-", "@", "\t", "\r"];

function cell(value: unknown): string {
  let text = value === null || value === undefined ? "" : String(value);
  if (FORMULA_LEADS.some((lead) => text.startsWith(lead))) text = `'${text}`;
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

const CONTACT_COLUMNS: [string, (r: SavedRow) => unknown][] = [
  ["name", (r) => r.name], ["title", (r) => r.title], ["seniority", (r) => r.seniority],
  ["email", (r) => r.email], ["email_status", (r) => r.email_status], ["phone", (r) => r.phone],
  ["company_name", (r) => r.company_name], ["company_domain", (r) => r.domain],
  ["country", (r) => r.country], ["region", (r) => r.region], ["city", (r) => r.city],
];

const ACCOUNT_COLUMNS: [string, (r: SavedRow) => unknown][] = [
  ["company_name", (r) => r.name], ["domain", (r) => r.domain], ["industry", (r) => r.industry],
  ["employee_count", (r) => r.employee_count], ["country", (r) => r.country],
  ["region", (r) => r.region], ["city", (r) => r.city],
];

export function savedCsv(view: ProspectView, rows: SavedRow[]): string {
  const columns = view === "contacts" ? CONTACT_COLUMNS : ACCOUNT_COLUMNS;
  const lines = [columns.map(([name]) => name).join(",")];
  for (const row of rows) lines.push(columns.map(([, get]) => cell(get(row))).join(","));
  return "﻿" + lines.join("\r\n");
}

export function downloadSavedCsv(view: ProspectView, rows: SavedRow[]): void {
  const blob = new Blob([savedCsv(view, rows)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `prospector-saved-${view}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked later, never on this task: the browser reads the URL after `click()` returns.
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
