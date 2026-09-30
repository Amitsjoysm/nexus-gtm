import { useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Skeleton,
  Textarea,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DncBadge } from "@/components/engagement/DncBadge";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { readCsvText } from "@/lib/readCsvText";
import type { DoNotContactEntry } from "@/lib/types";
import styles from "./DoNotContactPage.module.css";

/** Rows per request; mirrors `BULK_MAX` in `routers/engagement_suppression.py`. */
const BATCH = 1000;

const EMAIL = /^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9-]+(?:\.[a-z0-9-]+)+$/;
const DOMAIN = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}$/;

interface ParsedList {
  fileName: string;
  emails: string[];
  domains: string[];
  /** Cells that held neither, as written: a header row, a name, a note. */
  skipped: string[];
}

/** An address or a domain from one token, or null. The server re-checks everything it is sent. */
function readToken(raw: string): { kind: "email" | "domain"; value: string } | null {
  const token = raw.trim().replace(/^["'<(]+|[">)',;.]+$/g, "").toLowerCase();
  if (!token) return null;
  if (token.includes("@") && !token.startsWith("@")) {
    return EMAIL.test(token) ? { kind: "email", value: token } : null;
  }
  const domain = token
    .replace(/^@/, "")
    .replace(/^[a-z][a-z0-9+.-]*:\/\//, "")
    .split(/[/?#]/)[0]
    .replace(/\.$/, "")
    .replace(/^www\./, "");
  return DOMAIN.test(domain) ? { kind: "domain", value: domain } : null;
}

/**
 * Every address and domain in a pasted or uploaded list. A CSV cell, a line of a text file and a
 * `Name <address>` pair all work; a cell with neither is reported, never guessed at.
 */
function parseList(text: string, fileName: string): ParsedList {
  const emails = new Set<string>();
  const domains = new Set<string>();
  const skipped: string[] = [];
  for (const cell of text.split(/[\r\n,;\t]+/)) {
    const trimmed = cell.trim();
    if (!trimmed) continue;
    const found = trimmed.split(/\s+/).map(readToken).filter((t) => t !== null);
    if (found.length === 0) {
      skipped.push(trimmed);
      continue;
    }
    for (const t of found) (t.kind === "email" ? emails : domains).add(t.value);
  }
  return { fileName, emails: [...emails], domains: [...domains], skipped };
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/**
 * The workspace's do-not-contact list (spec §9, D7, D11).
 *
 * Every member sees it and can block an address or a whole domain, one at a time or from an
 * uploaded list, because an SDR about to email someone needs to know. Lifting a block is for
 * managers and needs a note, kept with the record. An unsubscribe is permanent: it shows no Lift
 * action at all rather than one that fails.
 */
export function DoNotContactPage() {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const canLift = session?.role === "manager" || session?.role === "admin"
    || session?.role === "owner";
  const [query, setQuery] = useState("");
  const [includeLifted, setIncludeLifted] = useState(false);
  const [address, setAddress] = useState("");
  const [adding, setAdding] = useState(false);
  const [lifting, setLifting] = useState<DoNotContactEntry | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const fileInput = useRef<HTMLInputElement>(null);
  const [upload, setUpload] = useState<ParsedList | null>(null);
  const [takeEmails, setTakeEmails] = useState(true);
  const [takeDomains, setTakeDomains] = useState(true);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  const blocks = useApi<DoNotContactEntry[]>(
    (signal) => api.listDoNotContact({ q: query, include_lifted: includeLifted }, signal),
    [query, includeLifted],
  );

  async function add(event: FormEvent) {
    event.preventDefault();
    const entry = address.trim();
    setAdding(true);
    try {
      const row = await api.addDoNotContact(entry);
      toast.success(
        "Blocked",
        row.kind === "domain"
          ? `No campaign will email anyone at ${row.email.slice(1)}.`
          : `${row.email} will not be emailed by any campaign.`,
      );
      setAddress("");
      blocks.refetch();
    } catch (err) {
      toast.error("Couldn't block that", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setAdding(false);
    }
  }

  async function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // choosing the same file again should reopen the preview
    if (!file) return;
    try {
      const parsed = parseList(await readCsvText(file), file.name);
      if (parsed.emails.length === 0 && parsed.domains.length === 0) {
        toast.error(`Nothing to block in ${file.name}`,
          "Put one email address or domain per line, or in a column of a CSV.");
        return;
      }
      setTakeEmails(parsed.emails.length > 0);
      // A contact export with a website column would block whole companies when the operator
      // meant the people, so domains start unticked whenever the file also has addresses.
      setTakeDomains(parsed.emails.length === 0);
      setUpload(parsed);
    } catch {
      toast.error("Couldn't read that file", "Save it as CSV or plain text and try again.");
    }
  }

  async function blockUploaded() {
    if (!upload) return;
    const entries = [...(takeEmails ? upload.emails : []), ...(takeDomains ? upload.domains : [])];
    if (entries.length === 0) return;
    const totals = { emails: 0, domains: 0, already: 0 };
    setProgress({ done: 0, total: entries.length });
    try {
      for (let i = 0; i < entries.length; i += BATCH) {
        const res = await api.bulkDoNotContact(entries.slice(i, i + BATCH));
        totals.emails += res.emails_blocked;
        totals.domains += res.domains_blocked;
        totals.already += res.already_blocked;
        setProgress({ done: Math.min(i + BATCH, entries.length), total: entries.length });
      }
      const parts = [
        totals.emails ? plural(totals.emails, "address", "addresses") : "",
        totals.domains ? plural(totals.domains, "domain") : "",
      ].filter(Boolean);
      toast.success(
        parts.length ? `Blocked ${parts.join(" and ")}` : "Nothing new to block",
        totals.already ? `${plural(totals.already, "entry was", "entries were")} already blocked.` : undefined,
      );
      setUpload(null);
    } catch (err) {
      // Batches before the failure are kept; the list below shows what landed.
      toast.error("The upload stopped part-way",
        err instanceof ApiError ? err.detail : "Some entries were blocked. Check the list and try again.");
    } finally {
      setProgress(null);
      blocks.refetch();
    }
  }

  async function lift() {
    if (!lifting) return;
    setBusy(true);
    try {
      await api.liftDoNotContact(lifting.id, note.trim());
      toast.success("Block lifted", `${label(lifting)} can be contacted again.`);
      setLifting(null);
      setNote("");
      blocks.refetch();
    } catch (err) {
      toast.error("Couldn't lift the block", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<DoNotContactEntry>[] = [
    { key: "email", header: "Address or domain", sortValue: (b) => b.email,
      render: (b) => b.kind === "domain" ? (
        <span className={styles.entry}>
          <Badge tone="neutral">Domain</Badge>
          <span>Everyone at <span className={styles.mono}>{b.email.slice(1)}</span></span>
        </span>
      ) : <span className={styles.mono}>{b.email}</span> },
    { key: "reason", header: "Reason", sortValue: (b) => b.reason,
      render: (b) => <DncBadge reason={b.reason} /> },
    { key: "created_at", header: "Since", hideOnMobile: true, sortValue: (b) => b.created_at,
      render: (b) => new Date(b.created_at).toLocaleDateString() },
    { key: "status", header: "Status", hideOnMobile: true, sortValue: (b) => b.lifted_at ?? "",
      render: (b) => b.lifted_at
        ? <Badge tone="neutral" title={b.lift_note}>Lifted {new Date(b.lifted_at).toLocaleDateString()}</Badge>
        : <Badge tone="danger" dot>Active</Badge> },
    { key: "actions", header: "", render: (b) => canLift && b.liftable ? (
        <Button size="sm" variant="secondary" onClick={() => setLifting(b)}>Lift</Button>
      ) : null },
  ];

  const selected = upload
    ? (takeEmails ? upload.emails.length : 0) + (takeDomains ? upload.domains.length : 0)
    : 0;

  return (
    <div>
      <PageHeader
        eyebrow={<Link to="/mailboxes" className={styles.back}><Icons.ChevronLeftIcon /> My mailboxes</Link>}
        title="Do not contact"
        description="Addresses and domains no campaign will email. Unsubscribes are permanent; a clear no, a bounce or a manual block can be lifted by a manager with a note."
      />

      <Card padding="lg" className={styles.tools}>
        <div className={styles.addRow}>
          <form className={styles.addForm} onSubmit={add}>
            <Field label="Block an address or a domain"
              hint="A domain blocks everyone there, including its subdomains.">
              <Input value={address} onChange={(e) => setAddress(e.target.value)}
                placeholder="name@company.com or company.com" autoComplete="off" required />
            </Field>
            <Button type="submit" loading={adding} disabled={!address.trim()}>Block</Button>
          </form>
          <div className={styles.upload}>
            <input ref={fileInput} type="file" accept=".csv,.txt,text/csv,text/plain"
              className={styles.fileInput} onChange={chooseFile} tabIndex={-1} aria-hidden="true" />
            <Button variant="secondary" iconLeft={<Icons.UploadIcon />}
              onClick={() => fileInput.current?.click()}>
              Upload a list
            </Button>
            <p className={styles.uploadHint}>CSV or text: addresses, domains, or both.</p>
          </div>
        </div>
        <div className={styles.filters}>
          <Field label="Search" hideLabel>
            <Input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Search addresses and domains…" />
          </Field>
          <label className={styles.toggle}>
            <input type="checkbox" checked={includeLifted}
              onChange={(e) => setIncludeLifted(e.target.checked)} />
            Show lifted blocks
          </label>
        </div>
      </Card>

      <DataState
        state={blocks}
        errorTitle="Couldn't load the do-not-contact list"
        skeleton={<Skeleton width="100%" height={240} />}
        isEmpty={(rows) => rows.length === 0}
        empty={<EmptyState icon={<Icons.ShieldCheckIcon />} title="Nobody is blocked"
          description="Unsubscribes, clear no replies and bounces appear here automatically." />}
      >
        {(rows) => <DataTable columns={columns} rows={rows} getRowKey={(b) => b.id} caption="Do-not-contact list" />}
      </DataState>

      <Modal
        open={upload !== null}
        onClose={() => { if (!progress) setUpload(null); }}
        title={`Block from ${upload?.fileName ?? "your list"}`}
        description="Choose what to block. Nothing changes until you confirm."
        footer={
          <>
            <Button variant="ghost" onClick={() => setUpload(null)} disabled={progress !== null}>
              Cancel
            </Button>
            <Button onClick={blockUploaded} loading={progress !== null} disabled={selected === 0}>
              {progress
                ? `Blocking ${progress.done.toLocaleString()} of ${progress.total.toLocaleString()}`
                : `Block ${selected.toLocaleString()}`}
            </Button>
          </>
        }
      >
        {upload && (
          <div className={styles.choices}>
            {upload.emails.length > 0 && (
              <label className={styles.choice}>
                <input type="checkbox" checked={takeEmails}
                  onChange={(e) => setTakeEmails(e.target.checked)} />
                <span className={styles.choiceText}>
                  <span className={styles.choiceTitle}>
                    {plural(upload.emails.length, "email address", "email addresses")}
                  </span>
                  <span className={styles.choiceHint}>Only these people.</span>
                  <span className={styles.sample}>{upload.emails.slice(0, 3).join(", ")}
                    {upload.emails.length > 3 ? ", …" : ""}</span>
                </span>
              </label>
            )}
            {upload.domains.length > 0 && (
              <label className={styles.choice}>
                <input type="checkbox" checked={takeDomains}
                  onChange={(e) => setTakeDomains(e.target.checked)} />
                <span className={styles.choiceText}>
                  <span className={styles.choiceTitle}>{plural(upload.domains.length, "domain")}</span>
                  <span className={styles.choiceHint}>
                    Everyone at these companies.
                    {upload.emails.length > 0 &&
                      " Left unticked because the file also has addresses: a website column in a contact export would block whole companies."}
                  </span>
                  <span className={styles.sample}>{upload.domains.slice(0, 3).join(", ")}
                    {upload.domains.length > 3 ? ", …" : ""}</span>
                </span>
              </label>
            )}
            {upload.skipped.length > 0 && (
              <p className={styles.skipped}>
                Skipping {plural(upload.skipped.length, "entry", "entries")} that{" "}
                {upload.skipped.length === 1 ? "is" : "are"} neither an address nor a domain:{" "}
                <span className={styles.mono}>{upload.skipped.slice(0, 3).join(", ")}</span>
                {upload.skipped.length > 3 ? ", …" : ""}
              </p>
            )}
          </div>
        )}
      </Modal>

      <Modal
        open={lifting !== null}
        onClose={() => setLifting(null)}
        title={`Lift the block on ${lifting ? label(lifting) : ""}?`}
        description="Campaigns may email this address again. Your note is kept with the record."
        footer={
          <>
            <Button variant="ghost" onClick={() => setLifting(null)}>Cancel</Button>
            <Button variant="danger" onClick={lift} loading={busy} disabled={note.trim().length < 5}>
              Lift block
            </Button>
          </>
        }
      >
        <Field label="Why" hint="For example: they asked to hear from us again on a call.">
          <Textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Modal>
    </div>
  );
}

function label(b: DoNotContactEntry): string {
  return b.kind === "domain" ? `everyone at ${b.email.slice(1)}` : b.email;
}

export default DoNotContactPage;
