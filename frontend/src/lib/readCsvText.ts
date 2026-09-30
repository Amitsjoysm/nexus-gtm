/**
 * Read the file the way the SERVER will read it: UTF-8, falling back to windows-1252.
 *
 * `File.text()` always decodes UTF-8, and Excel on Windows exports cp1252 — so a header like
 * `Société` came back as `Soci<?>t<?>` in the preview while `_decode` in `nexus/imports/csv_ingest.py`
 * read it correctly. The mapping is keyed BY HEADER TEXT, so the two spellings never met: the column
 * the operator mapped to Company name was, as far as the server was concerned, not mapped at all.
 * Every row was then skipped for having no company name and no website, and the upload reported
 * nothing imported. Reported 2026-09-16 as "uploads in accounts are failing".
 *
 * U+FFFD is the tell: it is what a decoder substitutes for bytes it cannot read, and it cannot occur
 * in a correctly-decoded UTF-8 file unless the file itself contains one.
 */
export async function readCsvText(file: File): Promise<string> {
  const bytes = await file.arrayBuffer();
  const utf8 = new TextDecoder("utf-8").decode(bytes);
  if (!utf8.includes("�")) return utf8;
  try {
    return new TextDecoder("windows-1252").decode(bytes);
  } catch {
    return utf8; // a browser without that label is still better served by the UTF-8 attempt
  }
}
