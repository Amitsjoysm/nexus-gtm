/**
 * Tiny dependency-free CSV reader for client-side preview. The real import streams the raw
 * File to the server; this just parses enough to show headers + the first rows and to drive
 * column mapping. Handles quoted fields, embedded commas/newlines, `""` escapes, and CRLF/LF/CR
 * line endings via a small state machine.
 */
export interface ParsedCsv {
  headers: string[];
  rows: string[][];
}

export function parseCsv(text: string, opts: { maxRows?: number } = {}): ParsedCsv {
  const maxRows = opts.maxRows ?? Number.POSITIVE_INFINITY;
  // Strip a leading UTF-8 BOM if present.
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);

  const records: string[][] = [];
  let record: string[] = [];
  let field = "";
  let inQuotes = false;
  let dirty = false; // the current record has seen any content/separator

  const endField = () => {
    record.push(field);
    field = "";
  };
  const endRecord = () => {
    endField();
    records.push(record);
    record = [];
    dirty = false;
  };

  const n = text.length;
  let i = 0;
  while (i < n) {
    const ch = text[i];

    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i += 1;
        continue;
      }
      field += ch;
      i += 1;
      continue;
    }

    if (ch === '"') {
      inQuotes = true;
      dirty = true;
      i += 1;
      continue;
    }
    if (ch === ",") {
      endField();
      dirty = true;
      i += 1;
      continue;
    }
    if (ch === "\r" || ch === "\n") {
      endRecord();
      if (ch === "\r" && text[i + 1] === "\n") i += 1;
      i += 1;
      if (records.length > maxRows) break;
      continue;
    }

    field += ch;
    dirty = true;
    i += 1;
  }

  // Flush a trailing record that wasn't terminated by a newline.
  if (dirty || field !== "" || record.length > 0) endRecord();

  const headers = records.length > 0 ? records[0] : [];
  const rows = records
    .slice(1)
    .filter((r) => r.some((cell) => cell.trim() !== ""));

  return { headers, rows };
}
