import { useEffect, useState } from "react";
import { Button, Icons, Skeleton } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { ImportAction, ImportActionQuote, ImportActionResult } from "@/lib/types";
import styles from "./ImportFollowUp.module.css";

const COPY: Record<ImportAction, {
  title: string;
  noun: (n: number) => string;
  verb: (n: number) => string;
  none: string;
  explain: string;
}> = {
  verify_emails: {
    title: "Verify their emails",
    noun: (n) => `${formatNumber(n)} address${n === 1 ? "" : "es"}`,
    verb: (n) => `Verify ${formatNumber(n)} email${n === 1 ? "" : "s"}`,
    none: "Every imported address already has a verdict.",
    explain: "Checks each address with the mailbox verifier. One that proves dead is removed.",
  },
  fill_firmographics: {
    title: "Fill missing firmographics",
    noun: (n) => `${formatNumber(n)} account${n === 1 ? "" : "s"}`,
    verb: (n) => `Fill ${formatNumber(n)} account${n === 1 ? "" : "s"}`,
    none: "Every imported account already has an industry, size and country.",
    explain: "Looks up industry, size and country where they are blank. Nothing you imported is overwritten.",
  },
};

export interface ImportFollowUpProps {
  action: ImportAction;
  /** The rows the import wrote (the server sends at most 1,000). */
  ids: string[];
  /** Rows the import wrote in all, to say when only the first 1,000 can be acted on. */
  written: number;
}

/**
 * The next step after an import, priced before it runs.
 *
 * The quote charges nothing, so it is asked for as soon as the result shows; the button then names
 * exactly what it will do and what it costs. Both actions are the product's existing charged work
 * (the verifier Prospector's Verify uses, the enricher behind an account's Enrich), so the price is
 * the one the rate card already sets.
 */
export function ImportFollowUp({ action, ids, written }: ImportFollowUpProps) {
  const api = useApiClient();
  const copy = COPY[action];
  const [quote, setQuote] = useState<ImportActionQuote | null>(null);
  const [quoteError, setQuoteError] = useState("");
  const [running, setRunning] = useState(false);
  const [done, setDone] = useState<ImportActionResult | null>(null);
  const [runError, setRunError] = useState("");

  useEffect(() => {
    if (ids.length === 0) return;
    const ctrl = new AbortController();
    api
      .quoteImportAction(action, ids, ctrl.signal)
      .then(setQuote)
      .catch((err) => {
        if (ctrl.signal.aborted) return;
        setQuoteError(err instanceof ApiError ? err.detail : "Couldn't work out the cost.");
      });
    return () => ctrl.abort();
  }, [api, action, ids]);

  if (ids.length === 0) return null;

  async function run() {
    setRunning(true);
    setRunError("");
    try {
      setDone(await api.runImportAction(action, ids));
    } catch (err) {
      setRunError(err instanceof ApiError ? err.detail : "It didn't start. Nothing was charged.");
    } finally {
      setRunning(false);
    }
  }

  const free = quote ? quote.count - quote.charged : 0;

  return (
    <section className={styles.panel} aria-labelledby={`follow-${action}`}>
      <div className={styles.head}>
        <h3 id={`follow-${action}`} className={styles.title}>
          {copy.title}
        </h3>
        <p className={styles.explain}>{copy.explain}</p>
      </div>

      {quoteError ? (
        <p className={styles.error} role="alert">
          {quoteError}
        </p>
      ) : !quote ? (
        <Skeleton width="60%" height={16} />
      ) : done ? (
        <p className={styles.done} role="status">
          <Icons.CheckIcon aria-hidden="true" />
          <span>
            {done.mode === "inline"
              ? `Checked ${copy.noun(done.count)}${done.charged ? `, ${formatNumber(done.charged)} charged` : ""}. Their status is on the list.`
              : `Working on ${copy.noun(done.count)} in the background. The list updates as each one finishes.`}
          </span>
        </p>
      ) : quote.count === 0 ? (
        <p className={styles.explain}>{copy.none}</p>
      ) : (
        <div className={styles.offer}>
          <dl className={styles.price}>
            <div>
              <dt>Rows</dt>
              <dd>
                {copy.noun(quote.count)}
                {free > 0 && <span className={styles.aside}> ({formatNumber(free)} free)</span>}
              </dd>
            </div>
            <div>
              <dt>Cost</dt>
              <dd>
                {formatNumber(quote.total_credits)} credit{quote.total_credits === 1 ? "" : "s"}
                {quote.charged > 0 && (
                  <span className={styles.aside}>
                    {" "}
                    ({formatNumber(quote.credits_per_row)} each)
                  </span>
                )}
              </dd>
            </div>
            {quote.balance !== null && (
              <div>
                <dt>Balance</dt>
                <dd>{formatNumber(quote.balance)}</dd>
              </div>
            )}
          </dl>
          {written > ids.length && (
            <p className={styles.explain}>
              This covers the first {formatNumber(ids.length)} imported rows. Run it again from the
              list for the rest.
            </p>
          )}
          {!quote.allowed && (
            <p className={styles.error} role="alert">
              {quote.reason}
            </p>
          )}
          <div className={styles.actions}>
            <Button
              size="sm"
              variant="secondary"
              loading={running}
              disabled={!quote.allowed}
              onClick={() => void run()}
            >
              {copy.verb(quote.count)}
            </Button>
          </div>
          {runError && (
            <p className={styles.error} role="alert">
              {runError}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
