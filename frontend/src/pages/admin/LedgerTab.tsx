import { type FormEvent, useState } from "react";

import { useApiClient } from "@/app/AuthContext";
import { DataState } from "@/components/DataState";
import { Badge, Button, CardHeader, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import type { LedgerStatus, LedgerStoreStatus } from "@/lib/types";

import styles from "./LedgerTab.module.css";

/**
 * The training & insights ledger: what each store is doing, how much is waiting, and the two
 * actions an operator has — apply a store's schema, and erase a person everywhere.
 *
 * No connection string is ever rendered. They are entered in Provider keys, sealed there, and this
 * screen reports only state: configured, reachable, owns its schema, which versions are applied.
 */
export function LedgerTab() {
  const api = useApiClient();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const status = useApi<LedgerStatus>((signal) => api.ledgerStatus(signal), [nonce]);
  const [busy, setBusy] = useState("");
  const [email, setEmail] = useState("");

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

  async function erase(event: FormEvent) {
    event.preventDefault();
    if (!email.trim()) return;
    setBusy("erase");
    try {
      const result = await api.eraseLedgerPerson(email.trim());
      toast.success(`Queued: every store will be cleared of ${result.person_key.slice(0, 8)}…`);
      setEmail("");
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
        subtitle="What workspaces that opted in have contributed, where it is stored, and the two operator actions: apply a store's schema, and erase a person from every store."
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
                    {s.capture_enabled ? "" : "Runtime settings → Mailboxes & engagement"}
                  </dd>
                </div>
                <div>
                  <dt>Pseudonymisation secret</dt>
                  <dd>
                    <Badge tone={s.pseudonym_secret_configured ? "success" : "warning"} dot>
                      {s.pseudonym_secret_configured ? "Stored" : "Missing"}
                    </Badge>
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

            <section className={styles.section} aria-labelledby="ledger-erase">
              <h3 id="ledger-erase" className={styles.heading}>Erase a person</h3>
              <p className={styles.hint}>
                Removes every event, training row, fact and profile about this address from all
                three stores, including what has not been shipped yet. The address itself is never
                stored in the job or the audit row — only the key derived from it.
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
            </section>
          </>
        )}
      </DataState>
    </div>
  );
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
        {store.detail && <p className={styles.detail}>{store.detail}</p>}
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
