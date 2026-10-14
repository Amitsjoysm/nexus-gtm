import { type FormEvent, useState } from "react";

import { useApiClient } from "@/app/AuthContext";
import { DataState } from "@/components/DataState";
import { Badge, Button, CardHeader, Field, Input, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import type { LedgerErasure, LedgerStatus, LedgerStoreStatus } from "@/lib/types";

import styles from "./LedgerTab.module.css";

const STORES = ["archive", "training", "insights"] as const;
type StoreName = (typeof STORES)[number];

/**
 * The training & insights ledger: what each store is doing, how much is waiting, and the operator
 * actions — connect the stores, apply a store's schema, and erase a person everywhere.
 *
 * Connection strings are typed here and stored as Provider keys, sealed. None is ever rendered
 * back: the screen reports only state (configured, reachable, owns its schema, versions applied)
 * and where the value in use came from, with its last four characters.
 */
export function LedgerTab() {
  const api = useApiClient();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const status = useApi<LedgerStatus>((signal) => api.ledgerStatus(signal), [nonce]);
  const [busy, setBusy] = useState("");
  const [email, setEmail] = useState("");
  const [dsns, setDsns] = useState<Record<StoreName, string>>({
    archive: "", training: "", insights: "",
  });

  async function applySchema(store: string) {
    setBusy(store);
    try {
      const result = await api.applyLedgerSchema(store);
      toast.success(
        result.applied.length
          ? `${store}: applied ${result.applied.join(", ")}`
          : `${store}: already up to date`,
      );
      setNonce((n) => n + 1);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : `Could not apply the ${store} schema`);
    } finally {
      setBusy("");
    }
  }

  async function saveConnections(event: FormEvent) {
    event.preventDefault();
    const body: Partial<Record<`${StoreName}_dsn`, string>> = {};
    for (const store of STORES) {
      if (dsns[store].trim()) body[`${store}_dsn`] = dsns[store].trim();
    }
    if (!Object.keys(body).length) return;
    setBusy("setup");
    try {
      await api.saveLedgerSetup(body);
      setDsns({ archive: "", training: "", insights: "" });
      toast.success("Connection strings saved", "Apply each store's schema once it reads reachable.");
      setNonce((n) => n + 1);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not save the connection strings");
    } finally {
      setBusy("");
    }
  }

  async function generateSecret() {
    if (
      !window.confirm(
        "Create the pseudonymisation secret? It is generated once and kept sealed. It cannot be " +
          "replaced from this screen later, because everything archived is sealed with it.",
      )
    ) {
      return;
    }
    setBusy("secret");
    try {
      await api.saveLedgerSetup({ generate_pseudonym_secret: true });
      toast.success("Pseudonymisation secret created");
      setNonce((n) => n + 1);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not create the secret");
    } finally {
      setBusy("");
    }
  }

  async function erase(event: FormEvent) {
    event.preventDefault();
    if (!email.trim()) return;
    setBusy("erase");
    try {
      await api.eraseLedgerPerson(email.trim());
      toast.success(
        "Erasure queued",
        "Removing this person from the outbox and every store. They will not be collected again.",
      );
      setEmail("");
      setNonce((n) => n + 1);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not queue the erasure");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className={styles.stack}>
      <CardHeader
        title="Training &amp; insights ledger"
        subtitle="What workspaces that opted in have contributed, where it is stored, and the operator actions: connect the stores, apply a store's schema, and erase a person from every store."
      />
      <DataState
        state={status}
        errorTitle="Couldn't load the ledger status"
        skeleton={<Skeleton width="100%" height={320} />}
      >
        {(s) => (
          <>
            <section className={styles.section} aria-labelledby="ledger-collection">
              <h3 id="ledger-collection" className={styles.heading}>Collection</h3>
              <dl className={styles.facts}>
                <div>
                  <dt>Capture</dt>
                  <dd>
                    <Badge tone={s.capture_enabled ? "success" : "warning"} dot>
                      {s.capture_enabled ? "On" : "Off"}
                    </Badge>{" "}
                    {s.capture_enabled ? "" : "Configuration → Mailboxes & engagement"}
                  </dd>
                </div>
                <div>
                  <dt>Pseudonymisation secret</dt>
                  <dd className={styles.inline}>
                    <Badge tone={s.pseudonym_secret_configured ? "success" : "warning"} dot>
                      {s.pseudonym_secret_configured ? "Stored" : "Missing"}
                    </Badge>
                    {!s.pseudonym_secret_configured && (
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={busy === "secret"}
                        disabled={busy !== "" && busy !== "secret"}
                        onClick={() => void generateSecret()}
                      >
                        Generate secret
                      </Button>
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Workspaces</dt>
                  <dd>
                    {s.consented_workspaces} contributing · {s.opted_out_workspaces} off ·{" "}
                    {s.undecided_workspaces} not asked
                  </dd>
                </div>
                <div>
                  <dt>Waiting to ship</dt>
                  <dd>
                    {s.outbox.waiting === 0
                      ? "Nothing waiting"
                      : `${s.outbox.waiting} events, oldest ${Math.floor(
                          s.outbox.oldest_age_s / 3600,
                        )}h${s.outbox.retrying ? `, ${s.outbox.retrying} retrying` : ""}`}
                  </dd>
                </div>
                <div>
                  <dt>Datasets last built</dt>
                  <dd>
                    {s.last_built_at ? new Date(s.last_built_at).toLocaleString() : "Never"}
                  </dd>
                </div>
              </dl>
            </section>

            <section className={styles.section} aria-labelledby="ledger-stores">
              <h3 id="ledger-stores" className={styles.heading}>Stores</h3>
              <ul className={styles.stores}>
                {s.stores.map((store) => (
                  <StoreCard
                    key={store.store}
                    store={store}
                    busy={busy === store.store}
                    onApply={() => void applySchema(store.store)}
                  />
                ))}
              </ul>
            </section>

            <section className={styles.section} aria-labelledby="ledger-connection">
              <h3 id="ledger-connection" className={styles.heading}>Connection</h3>
              <p className={styles.hint}>
                Each store is its own PostgreSQL database, reached with a role that owns the
                nexus_ledger schema and nothing else. Saved values are sealed and never shown
                again; leave a box blank to keep what is stored.
              </p>
              <form className={styles.connection} onSubmit={saveConnections}>
                {STORES.map((name) => {
                  const store = s.stores.find((row) => row.store === name);
                  return (
                    <Field
                      key={name}
                      label={`${name[0].toUpperCase()}${name.slice(1)} store`}
                      hint={sourceHint(store)}
                    >
                      <Input
                        type="password"
                        value={dsns[name]}
                        onChange={(e) => setDsns((current) => ({ ...current, [name]: e.target.value }))}
                        placeholder="postgresql://user:password@host:5432/postgres"
                        autoComplete="new-password"
                        spellCheck={false}
                      />
                    </Field>
                  );
                })}
                <div>
                  <Button
                    type="submit"
                    loading={busy === "setup"}
                    disabled={(busy !== "" && busy !== "setup") || !STORES.some((n) => dsns[n].trim())}
                  >
                    Save connection strings
                  </Button>
                </div>
              </form>
            </section>

            <section className={styles.section} aria-labelledby="ledger-erase">
              <h3 id="ledger-erase" className={styles.heading}>Erase a person</h3>
              <p className={styles.hint}>
                Removes every event, training row, fact and profile that names this address, from
                the outbox and all three stores, including what has not been shipped yet, and stops
                anything about them being collected again. The address is never stored: only keys
                derived from it.
              </p>
              <form className={styles.row} onSubmit={erase}>
                <input
                  className={styles.input}
                  type="email"
                  value={email}
                  placeholder="person@example.com"
                  aria-label="Email address to erase"
                  onChange={(e) => setEmail(e.target.value)}
                />
                <Button type="submit" variant="danger" disabled={busy === "erase" || !email.trim()}>
                  {busy === "erase" ? "Queueing…" : "Erase everywhere"}
                </Button>
              </form>
              {s.recent_erasures.length > 0 && (
                <ul className={styles.erasures} aria-label="Recent erasures">
                  {s.recent_erasures.map((row) => (
                    <ErasureRow key={row.id} row={row} />
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </DataState>
    </div>
  );
}

function sourceHint(store: LedgerStoreStatus | undefined): string {
  if (!store || !store.source) return "Not set.";
  if (store.source === "panel") return `Saved here, ends …${store.hint}.`;
  return "Set in the server environment. A value saved here takes over.";
}

function StoreCard({
  store,
  busy,
  onApply,
}: {
  store: LedgerStoreStatus;
  busy: boolean;
  onApply: () => void;
}) {
  const state = !store.configured
    ? "Not configured"
    : !store.reachable
      ? "Unreachable"
      : store.pending.length
        ? `${store.pending.length} version(s) pending`
        : "Up to date";
  const tone = !store.configured || !store.reachable
    ? "warning"
    : store.pending.length
      ? "neutral"
      : "success";
  return (
    <li className={styles.store}>
      <div>
        <strong>{store.store}</strong>{" "}
        <Badge tone={tone} dot>
          {state}
        </Badge>
        {store.detail && (
          <p className={styles.detail}>
            {store.configured ? store.detail : "Add its connection string under Connection below."}
          </p>
        )}
        <p className={styles.detail}>
          Applied: {store.applied.length ? store.applied.join(", ") : "none"}
          {store.owns_schema ? " · owns nexus_ledger" : ""}
        </p>
      </div>
      <Button
        variant="secondary"
        disabled={busy || !store.configured || !store.pending.length}
        onClick={onApply}
      >
        {busy ? "Applying…" : "Apply schema"}
      </Button>
    </li>
  );
}

const PLACES = ["outbox", "archive", "training", "insights"];

/** One erasure: whether it finished, what it removed, and what it found left (expected: none). */
function ErasureRow({ row }: { row: LedgerErasure }) {
  const removed = PLACES.filter((place) => place in row.deleted)
    .map((place) => `${place} ${row.deleted[place]}`)
    .join(", ");
  const left = PLACES.filter((place) => (row.remaining[place] ?? 0) !== 0);
  const tone = row.state === "done" && !left.length
    ? "success"
    : row.state === "waiting"
      ? "neutral"
      : "warning";
  const label = row.state === "waiting"
    ? "Waiting for the worker"
    : row.state === "incomplete"
      ? `Incomplete, retrying: ${Object.keys(row.errors).join(", ")}`
      : left.length
        ? `Rows left in ${left.join(", ")}`
        : "Done, nothing left";
  return (
    <li className={styles.erasure}>
      <div className={styles.inline}>
        <Badge tone={tone} dot>
          {label}
        </Badge>
        <span className={styles.detail}>
          {new Date(row.requested_at).toLocaleString()} · key {row.key}
        </span>
      </div>
      {removed && <p className={styles.detail}>Removed: {removed}</p>}
      {row.unconfigured.length > 0 && (
        <p className={styles.detail}>
          Not configured, so nothing was there: {row.unconfigured.join(", ")}
        </p>
      )}
      {row.note && <p className={styles.detail}>{row.note}</p>}
    </li>
  );
}
