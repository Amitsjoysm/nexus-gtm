import { formatDate } from "@/lib/format";
import type { Contact, FieldSource, FieldSources } from "@/lib/types";
import styles from "./SourceNote.module.css";

/**
 * Where a stored value came from, as one quiet line a rep can check (SDR feedback, 2026-10-26:
 * "show the source of every account and contact so we can validate it").
 *
 * The evidence is a link when there is one: the LinkedIn page, the profile, the search hit. A value
 * written before sources were kept says so, with how its record arrived, rather than guessing.
 */

const SOURCE_LABELS: Record<string, string> = {
  linkedin: "LinkedIn",
  pool: "LinkedIn, found earlier",
  web_search: "Web search",
  enrichment_web: "AI read the web",
  source_database: "Data provider",
  b2b_actor: "Company data provider",
  infojoy: "InfoJoy",
  csv_import: "CSV import",
  crm: "CRM",
  manual: "Typed by hand",
  shared_record: "Another workspace's find",
  // The email finders (`nexus/enrichment/providers.py`).
  pattern_verified: "Email finder",
  pattern: "Email pattern guess",
  search: "Found on the web",
  source_db: "Data provider",
  shared_people: "Found earlier by us",
};

/** How a record arrived (`accounts.source`, `contacts.enrichment_source`). */
const ARRIVED_LABELS: Record<string, string> = {
  csv_import: "a CSV import",
  auto_discovery: "daily discovery",
  discovery: "the Orchestrator",
  prospector: "Prospector",
  lookalike: "Find similar",
  similar_people: "Similar people",
  "sourcing:linkedin": "LinkedIn",
  "sourcing:pool": "LinkedIn",
  "sourcing:search": "web search",
  "sourcing:prospector": "Prospector",
  pattern_verified: "the email finder",
};

const CRM_NAMES: Record<string, string> = {
  hubspot: "HubSpot",
  salesforce: "Salesforce",
  pipedrive: "Pipedrive",
  zoho: "Zoho",
};

function crmName(raw: string | null | undefined): string {
  const key = (raw ?? "").toLowerCase();
  return CRM_NAMES[key] ?? (key ? key.charAt(0).toUpperCase() + key.slice(1) : "CRM");
}

export function sourceLabel(src: FieldSource): string {
  if (src.source === "crm") return crmName(src.detail);
  if (src.source === "linkedin" && src.ref?.includes("/in/")) return "LinkedIn profile";
  if (src.source === "linkedin" && src.ref) return "LinkedIn page";
  return SOURCE_LABELS[src.source] ?? src.source.replace(/_/g, " ");
}

export function arrivedLabel(via: string | undefined): string {
  if (!via || via === "unknown") return "an unrecorded route";
  if (via.startsWith("crm:")) return crmName(via.slice(4));
  return ARRIVED_LABELS[via] ?? via.replace(/[_:]/g, " ");
}

/** "Oct 26" this year, "Oct 26, 2025" before it: the year only when it tells the reader something. */
function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  if (date.getFullYear() !== new Date().getFullYear()) return formatDate(date);
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
}

/** Two entries say the same thing: one source, one piece of evidence, one moment. */
export function sameSource(a: FieldSource | undefined, b: FieldSource | undefined): boolean {
  if (!a || !b) return false;
  return (
    a.source === b.source &&
    (a.ref ?? "") === (b.ref ?? "") &&
    (a.at ?? "") === (b.at ?? "") &&
    (a.arrived_via ?? "") === (b.arrived_via ?? "") &&
    (a.refs ?? []).join(" ") === (b.refs ?? []).join(" ")
  );
}

/** The entry most of these fields share, so a block of facts from one source says so once. */
export function commonSource(entries: (FieldSource | undefined)[]): FieldSource | undefined {
  const present = entries.filter((e): e is FieldSource => Boolean(e));
  let best: FieldSource | undefined;
  let bestCount = 1;
  for (const candidate of present) {
    const count = present.filter((e) => sameSource(e, candidate)).length;
    if (count > bestCount) {
      best = candidate;
      bestCount = count;
    }
  }
  return best;
}

export interface SourceNoteProps {
  /** The field's entry from `field_sources`; nothing renders without one. */
  source: FieldSource | undefined;
  /** What the value is, for screen readers: "Industry". */
  field: string;
}

/** One line under a value: "LinkedIn page · Oct 20, 2026". */
export function SourceNote({ source, field }: SourceNoteProps) {
  if (!source) return null;
  if (source.source === "not_recorded") {
    return (
      <span className={styles.note}>
        Source not recorded · came with {arrivedLabel(source.arrived_via)}
      </span>
    );
  }
  const label = sourceLabel(source);
  const when = shortDate(source.at);
  const refs = source.refs && source.refs.length > 1 ? source.refs : null;
  return (
    <span className={styles.note} title={source.detail ?? undefined}>
      {refs ? (
        <details className={styles.refs}>
          {/* The date sits inside the summary: a summary renders as a block, so a date after the
              disclosure would drop to a line of its own. */}
          <summary aria-label={`${field}: ${label}, from ${refs.length} pages`}>
            {source.source === "enrichment_web"
              ? `AI read ${refs.length} web pages`
              : `${label} · ${refs.length} pages`}
            {when && <span className={styles.when}>{` · ${when}`}</span>}
          </summary>
          <ul>
            {refs.map((url) => (
              <li key={url}>
                <a href={url} target="_blank" rel="noreferrer noopener">
                  {hostOf(url)}
                </a>
              </li>
            ))}
          </ul>
        </details>
      ) : source.ref ? (
        <a
          className={styles.ref}
          href={source.ref}
          target="_blank"
          rel="noreferrer noopener"
          aria-label={`${field} source: ${label}, opens in a new tab`}
        >
          {label}
        </a>
      ) : (
        <span>{label}</span>
      )}
      {when && !refs && <span className={styles.when}>{` · ${when}`}</span>}
    </span>
  );
}

function hostOf(url: string): string {
  try {
    const u = new URL(url);
    return `${u.hostname.replace(/^www\./, "")}${u.pathname === "/" ? "" : u.pathname}`;
  } catch {
    return url;
  }
}

/**
 * Where a person was found and where their email came from, in one muted line:
 * "Found on LinkedIn profile · Email: Email finder, valid, checked at the mailbox".
 */
export function ContactSources({ contact }: { contact: Pick<Contact, "field_sources" | "email"> }) {
  const sources: FieldSources = contact.field_sources ?? {};
  const person = sources.person;
  const email = contact.email ? sources.email : undefined;
  if (!person && !email) return null;
  return (
    <span className={styles.contactLine}>
      {person && (
        <span>
          Found via <SourceNote source={person} field="Person" />
        </span>
      )}
      {email && (
        <span>
          Email: <SourceNote source={email} field="Email" />
          {email.detail && email.source !== "not_recorded" && (
            <span className={styles.detail}> ({email.detail})</span>
          )}
        </span>
      )}
    </span>
  );
}
