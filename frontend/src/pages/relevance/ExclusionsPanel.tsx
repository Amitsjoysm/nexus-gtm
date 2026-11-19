import { useCallback, useEffect, useId, useState } from "react";
import type { ChangeEvent } from "react";
import { Button, Card, Field, Icons, Input, Textarea, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import styles from "./ExclusionsPanel.module.css";

/**
 * The discovery exclusion list (2026-11-19): companies Populate, the daily discovery and the
 * Orchestrator never add, whatever the workspace holds. Asked as "an enterprise with 2,000+
 * companies and an exclusion list of domains": customers, competitors, partners, a CRM's open
 * pipeline. Entries are domains, websites or an address at the company; the server reads each and
 * names back what it could not. Long lists go in slices of the server's limit.
 */

const SLICE = 5000;
const PAGE = 50;
/** Split a paste or a file into entries: lines, commas, semicolons and tabs all separate. */
function entriesOf(text: string): string[] {
  return text
    .split(/[\n\r,;\t]+/)
    .map((s) => s.trim().replace(/^["']|["']$/g, ""))
    .filter(Boolean);
}

interface Row {
  domain: string;
  reason: string;
  created_at: string | null;
}

export function ExclusionsPanel() {
  const api = useApiClient();
  const toast = useToast();
  const bodyId = useId();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [adding, setAdding] = useState(false);
  const [unreadable, setUnreadable] = useState<{ shown: string[]; count: number } | null>(null);
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);

  const load = useCallback(
    async (offset: number, signal?: AbortSignal) => {
      setLoading(true);
      try {
        const page = await api.listExclusions({ q: q.trim() || undefined, limit: PAGE, offset },
          signal);
        setTotal(page.total);
        setRows((prev) => (offset === 0 ? page.items : [...prev, ...page.items]));
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        toast.error("Couldn't load the exclusion list",
          err instanceof ApiError ? err.detail : "Try again.");
      } finally {
        setLoading(false);
      }
    },
    [api, q, toast],
  );

  // The count shows on the closed card; the list loads with it and on every search.
  useEffect(() => {
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => void load(0, ctrl.signal), q ? 250 : 0);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [load, q]);

  async function add() {
    const entries = entriesOf(text);
    if (!entries.length) return;
    setAdding(true);
    setUnreadable(null);
    let added = 0;
    let already = 0;
    const missed: string[] = [];
    let missedCount = 0;
    try {
      for (let i = 0; i < entries.length; i += SLICE) {
        const out = await api.addExclusions(entries.slice(i, i + SLICE));
        added += out.added;
        already += out.already;
        missed.push(...out.unreadable);
        missedCount += out.unreadable_count;
        setTotal(out.total);
      }
      setText("");
      setUnreadable(missedCount ? { shown: missed.slice(0, 50), count: missedCount } : null);
      toast.success(
        added ? `${added.toLocaleString()} added to the exclusion list` : "Nothing new to add",
        already ? `${already.toLocaleString()} were already on it.` : undefined,
      );
      void load(0);
    } catch (err) {
      toast.error("Couldn't add to the list", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setAdding(false);
    }
  }

  async function onFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const read = await file.text();
    setText((prev) => (prev.trim() ? `${prev.trim()}\n${read}` : read));
    setOpen(true);
  }

  async function remove(domain: string) {
    setRemoving(domain);
    try {
      await api.removeExclusions([domain]);
      setRows((prev) => prev.filter((r) => r.domain !== domain));
      setTotal((n) => (n === null ? n : Math.max(0, n - 1)));
    } catch (err) {
      toast.error("Couldn't remove it", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setRemoving(null);
    }
  }

  const pending = entriesOf(text).length;

  return (
    <Card padding="lg" className={styles.panel}>
      <div className={styles.head}>
        <div>
          <h2 className={styles.title}>
            Excluded companies
            {total !== null && <span className={styles.count}>{total.toLocaleString()}</span>}
          </h2>
          <p className={styles.desc}>
            Discovery never adds these: your customers, competitors, partners or open pipeline.
            Accounts you already have are not changed.
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Close" : "Manage"}
        </Button>
      </div>

      {open && (
        <div id={bodyId} className={styles.body}>
          <Field
            label="Add companies"
            hint="Domains, websites or an email address at the company, one per line or separated by commas. Paste thousands at once."
          >
            <Textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={5}
              placeholder={"acme.com\nhttps://www.globex.io\nsomeone@initech.com"}
              disabled={adding}
            />
          </Field>
          <div className={styles.actions}>
            <label className={styles.upload}>
              <Icons.UploadIcon />
              <span>Read a CSV or text file</span>
              <input type="file" accept=".csv,.txt,text/csv,text/plain" onChange={onFile}
                className="sr-only" />
            </label>
            <Button onClick={add} loading={adding} disabled={!pending}>
              {pending ? `Exclude ${pending.toLocaleString()}` : "Exclude"}
            </Button>
          </div>
          {unreadable && (
            <p className={styles.unreadable} role="status">
              {unreadable.count.toLocaleString()} could not be read as a company domain (free
              email and personal sites are never excluded): {unreadable.shown.join(", ")}
              {unreadable.count > unreadable.shown.length ? ", …" : ""}
            </p>
          )}

          <div className={styles.listHead}>
            <Input
              type="search"
              aria-label="Search excluded companies"
              placeholder="Search the list"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          {rows.length === 0 && !loading ? (
            <p className={styles.empty}>
              {q ? `No excluded company matches “${q}”.` : "Nothing is excluded yet."}
            </p>
          ) : (
            <ul className={styles.list} aria-label="Excluded companies">
              {rows.map((r) => (
                <li key={r.domain} className={styles.row}>
                  <span className={styles.domain}>{r.domain}</span>
                  <span className={styles.reason}>
                    {r.reason === "deleted" ? "Deleted, not to be suggested again" : "On your list"}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    loading={removing === r.domain}
                    onClick={() => remove(r.domain)}
                    aria-label={`Allow discovery to offer ${r.domain} again`}
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {total !== null && rows.length < total && (
            <Button variant="secondary" size="sm" loading={loading}
              onClick={() => void load(rows.length)}>
              Show more ({(total - rows.length).toLocaleString()} left)
            </Button>
          )}
        </div>
      )}
    </Card>
  );
}
