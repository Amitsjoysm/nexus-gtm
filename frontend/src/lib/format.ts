/** Display formatters. Pure, locale-aware, null-safe. */

export function formatNumber(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat().format(n);
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/** Relative time like "3h ago", "2d ago". Accepts ISO strings or Date. */
export function timeAgo(input: string | Date | null | undefined): string {
  if (!input) return "—";
  const date = typeof input === "string" ? new Date(input) : input;
  const ms = date.getTime();
  if (Number.isNaN(ms)) return "—";
  const diff = Date.now() - ms;
  const sec = Math.round(diff / 1000);
  if (sec < 45) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mo = Math.round(day / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.round(mo / 12)}y ago`;
}

/**
 * When a signal happened: "3d ago". For a signal whose source gave no date, when we found it:
 * "found 3d ago". Undated signals are dated at collection, so without the word a two-year-old
 * article found yesterday would read as yesterday's news.
 */
export function signalWhen(sig: { occurred_at?: string | null; dated?: string | null }): string {
  const ago = timeAgo(sig.occurred_at);
  return sig.dated === "event" ? ago : `found ${ago}`;
}

/** The tooltip for `signalWhen`: the exact date, and what it means. */
export function signalWhenTitle(sig: { occurred_at?: string | null; dated?: string | null }): string {
  if (!sig.occurred_at) return "";
  const date = new Date(sig.occurred_at);
  if (Number.isNaN(date.getTime())) return "";
  const day = date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  return sig.dated === "event"
    ? `Happened ${day}`
    : `Found ${day}. The source did not say when this happened.`;
}

/** Relative time from an age in hours, e.g. 5 → "5h ago", 50 → "2d ago". */
export function formatAgeHours(hours: number | null | undefined): string {
  if (hours == null || Number.isNaN(hours)) return "—";
  if (hours < 1) return "just now";
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const day = Math.round(hours / 24);
  if (day < 30) return `${day}d ago`;
  const mo = Math.round(day / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.round(mo / 12)}y ago`;
}

export function formatDate(input: string | Date | null | undefined): string {
  if (!input) return "—";
  const date = typeof input === "string" ? new Date(input) : input;
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}

/** First letters of a name, for avatars. */
export function initials(name: string | null | undefined): string {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? "").join("") || "?";
}

/** Title-case a snake/kebab token, e.g. "funding_round" → "Funding Round". */
export function humanize(token: string | null | undefined): string {
  if (!token) return "";
  return token
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
