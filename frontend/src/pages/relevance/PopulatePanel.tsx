import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Icons, Input, WorkingIndicator } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { PopulateQuote, PopulateRun } from "@/lib/types";
import styles from "./PopulatePanel.module.css";

/**
 * "How many companies now?" — asked when an ICP is saved (product owner, 2026-09-24).
 *
 * Inline on the Relevance page rather than a modal: the answer is about the profile the person is
 * looking at, and a progress view that can outlive a dialog belongs on the page. The server checks
 * the balance for the whole count before buying anything and charges only for companies actually
 * added, so the cost line here says "up to".
 */

const PRESETS = [10, 20, 50, 100] as const;
const MAX = 500;
const POLL_MS = 3000;

type Choice = (typeof PRESETS)[number] | "custom";

const SOURCE_LABEL: Record<string, string> = {
  database: "already in our company database",
  linkedin: "found on LinkedIn",
  web: "found by web search",
};

const DISCARD_LABEL: Record<string, (n: number) => string> = {
  already_held: (n) => `${n} you already have`,
  no_website: (n) => `${n} had no website of their own`,
  duplicate: (n) => `${n} came up twice`,
  outside_icp: (n) => `${n} fell outside the ICP's size or countries`,
  low_fit: (n) => `${n} scored below the fit threshold`,
};

const NOTE_LABEL: Record<string, string> = {
  not_configured: "LinkedIn search isn't available yet, so web search was used.",
  failed: "LinkedIn search didn't respond, so web search was used.",
  no_industry_codes:
    "None of the ICP's industries matched a LinkedIn industry, so web search was used.",
  nothing_new: "LinkedIn has no more companies matching this ICP right now.",
};

function plural(n: number, one: string, many = `${one}s`) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

function sourceLine(sources: Record<string, number>): string {
  const parts = Object.entries(sources)
    .filter(([, n]) => n > 0)
    .map(([source, n]) => `${n.toLocaleString()} ${SOURCE_LABEL[source] ?? source}`);
  return parts.join(", ");
}

function shortfallLine(discarded: Record<string, number>): string {
  return Object.entries(discarded)
    .filter(([reason, n]) => n > 0 && DISCARD_LABEL[reason])
    .map(([reason, n]) => DISCARD_LABEL[reason](n))
    .join("; ");
}

/** What a refused start means, in words that say what to do next. */
function refusal(err: unknown, count: number): { text: string; billing: boolean } {
  if (!(err instanceof ApiError)) return { text: "Couldn't start. Please try again.", billing: false };
  if (err.switchState && err.switchState !== "enabled") {
    return {
      text: err.switchMessage.trim() || "Adding companies is unavailable right now.",
      billing: false,
    };
  }
  if (err.status === 402) {
    return {
      text: `Your plan or credit balance doesn't cover ${plural(count, "company", "companies")}.`,
      billing: true,
    };
  }
  return { text: err.detail || "Couldn't start. Please try again.", billing: false };
}

export interface PopulatePanelProps {
  /** Show the question. Set by a save that changed the ICP, or by the page's "Add companies". */
  asking: boolean;
  onClose: () => void;
}

export function PopulatePanel({ asking, onClose }: PopulatePanelProps) {
  const api = useApiClient();
  const [choice, setChoice] = useState<Choice>(20);
  const [custom, setCustom] = useState("");
  const [quote, setQuote] = useState<PopulateQuote | null>(null);
  const [quoteFailed, setQuoteFailed] = useState(false);
  const [starting, setStarting] = useState(false);
  const [refused, setRefused] = useState<{ text: string; billing: boolean } | null>(null);
  const [run, setRun] = useState<PopulateRun | null>(null);
  const [runLost, setRunLost] = useState(false);
  const segmentRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const customRef = useRef<HTMLInputElement | null>(null);
  // Focus follows a CLICK on "Custom" into the number box. Arrowing onto it must not: that would
  // pull focus out of the radio group halfway through keyboard navigation.
  const [focusCustom, setFocusCustom] = useState(false);
  useEffect(() => {
    if (focusCustom && choice === "custom") customRef.current?.focus();
    setFocusCustom(false);
  }, [focusCustom, choice]);

  const customCount = Number.parseInt(custom, 10);
  const count =
    choice === "custom"
      ? Number.isFinite(customCount) ? customCount : 0
      : choice;
  const countValid = count >= 1 && count <= MAX;
  const active = run?.status === "queued" || run?.status === "running";

  // A populate still running when the page is opened (or reloaded) picks its progress back up.
  useEffect(() => {
    const ctrl = new AbortController();
    api
      .getLatestPopulate(ctrl.signal)
      .then((latest) => {
        if (latest && (latest.status === "queued" || latest.status === "running")) setRun(latest);
      })
      .catch(() => {
        /* Nothing running is the common case; a failed look-up just shows no progress. */
      });
    return () => ctrl.abort();
  }, [api]);

  // The price of the chosen count, against the balance. Debounced for typing in the custom box.
  useEffect(() => {
    if (!asking || active || !countValid) {
      setQuote(null);
      return;
    }
    const ctrl = new AbortController();
    const id = window.setTimeout(() => {
      api
        .getPopulateQuote(count, ctrl.signal)
        .then((q) => {
          setQuote(q);
          setQuoteFailed(false);
        })
        .catch((err) => {
          if (!ctrl.signal.aborted) {
            setQuote(null);
            setQuoteFailed(!(err instanceof DOMException));
          }
        });
    }, choice === "custom" ? 300 : 0);
    return () => {
      ctrl.abort();
      window.clearTimeout(id);
    };
  }, [api, asking, active, count, countValid, choice]);

  // Follow a run until it finishes. Polling stops the moment it is done or failed.
  const runId = run?.id;
  useEffect(() => {
    if (!runId || !active) return;
    const ctrl = new AbortController();
    let misses = 0;
    const id = window.setInterval(() => {
      api
        .getPopulateRun(runId, ctrl.signal)
        .then((next) => {
          misses = 0;
          setRunLost(false);
          setRun(next);
        })
        .catch(() => {
          // One dropped poll is noise; several in a row is worth saying, without giving up.
          misses += 1;
          if (misses >= 3 && !ctrl.signal.aborted) setRunLost(true);
        });
    }, POLL_MS);
    return () => {
      ctrl.abort();
      window.clearInterval(id);
    };
  }, [api, runId, active]);

  const start = useCallback(async () => {
    if (!countValid || starting) return;
    setStarting(true);
    setRefused(null);
    try {
      setRun(await api.startPopulate(count));
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Already running: show that run instead of an error about it.
        const latest = await api.getLatestPopulate().catch(() => null);
        if (latest) setRun(latest);
        else setRefused({ text: err.detail, billing: false });
      } else {
        setRefused(refusal(err, count));
      }
    } finally {
      setStarting(false);
    }
  }, [api, count, countValid, starting]);

  function onSegmentKey(e: KeyboardEvent<HTMLButtonElement>, index: number) {
    const options: Choice[] = [...PRESETS, "custom"];
    let next = index;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (index + 1) % options.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp")
      next = (index - 1 + options.length) % options.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = options.length - 1;
    else return;
    e.preventDefault();
    setChoice(options[next]);
    segmentRefs.current[next]?.focus();
  }

  function dismiss() {
    setRun(null);
    setRefused(null);
    setRunLost(false);
    onClose();
  }

  // ---- progress -------------------------------------------------------------------------------
  if (run && active) {
    return (
      <Card padding="lg" className={styles.panel} aria-labelledby="populate-title">
        <h2 id="populate-title" className={styles.title}>
          Adding {plural(run.requested, "company", "companies")}
        </h2>
        <WorkingIndicator
          className={styles.working}
          label={run.status === "queued" ? "Waiting to start" : "Finding companies that match this ICP"}
          hint="Companies we already know come first. A LinkedIn search runs for the rest, and each search can take up to five minutes. You can leave this page; the companies will still be added."
          slowAfter={300}
          slowHint="Still searching. LinkedIn is slow today, but nothing has stopped."
        />
        {runLost && (
          <p className={styles.inlineNote} role="status">
            <Icons.AlertTriangleIcon aria-hidden="true" />
            Can't reach the server to check progress. The companies are still being added.
          </p>
        )}
      </Card>
    );
  }

  // ---- result ---------------------------------------------------------------------------------
  if (run && run.status === "done") {
    const sources = sourceLine(run.sources);
    const shortfall = run.delivered < run.requested ? shortfallLine(run.discarded) : "";
    const notes = Object.values(run.notes)
      .map((n) => NOTE_LABEL[n])
      .filter(Boolean);
    return (
      <Card padding="lg" className={styles.panel} aria-labelledby="populate-title">
        <div className={styles.head}>
          <h2 id="populate-title" className={styles.title} role="status">
            {run.delivered === 0
              ? "No new companies were added"
              : `Added ${plural(run.delivered, "company", "companies")}` +
                (run.delivered < run.requested ? ` of ${run.requested.toLocaleString()}` : "")}
          </h2>
          <Button variant="ghost" size="sm" onClick={dismiss}>
            Close
          </Button>
        </div>
        <div className={styles.body}>
          {sources && <p className={styles.line}>{sources}.</p>}
          {shortfall && <p className={styles.line}>Not added: {shortfall}.</p>}
          {notes.map((n) => (
            <p key={n} className={styles.line}>
              {n}
            </p>
          ))}
          <p className={styles.charge}>
            {run.delivered === 0
              ? "Nothing was charged."
              : `Charged for the ${plural(run.delivered, "company", "companies")} added, and nothing else.`}
          </p>
        </div>
        {run.delivered > 0 && (
          <div className={styles.actions}>
            <Link to="/accounts" className={styles.linkButton}>
              View accounts
              <Icons.ChevronRightIcon aria-hidden="true" />
            </Link>
          </div>
        )}
      </Card>
    );
  }

  if (run && run.status === "failed") {
    return (
      <Card padding="lg" className={styles.panel} aria-labelledby="populate-title">
        <div className={styles.head}>
          <h2 id="populate-title" className={styles.title}>
            Couldn't add companies
          </h2>
          <Button variant="ghost" size="sm" onClick={dismiss}>
            Close
          </Button>
        </div>
        <p className={cn(styles.inlineNote, styles.danger)} role="alert">
          <Icons.AlertTriangleIcon aria-hidden="true" />
          {run.error || "Something went wrong while searching."} Nothing was charged.
        </p>
        <div className={styles.actions}>
          <Button variant="secondary" iconLeft={<Icons.RefreshIcon />} onClick={() => setRun(null)}>
            Try again
          </Button>
        </div>
      </Card>
    );
  }

  if (!asking) return null;

  // ---- the question ---------------------------------------------------------------------------
  const options: Choice[] = [...PRESETS, "custom"];
  const perCompany = quote?.credits_per_company ?? 0;
  const affordable =
    quote && quote.balance != null && perCompany > 0
      ? Math.floor(quote.balance / perCompany)
      : null;

  return (
    <Card padding="lg" className={styles.panel} aria-labelledby="populate-title">
      <div className={styles.head}>
        <div>
          <h2 id="populate-title" className={styles.title}>
            Add companies that match this ICP now?
          </h2>
          <p className={styles.desc}>
            Companies we already know come first, then a LinkedIn search for the rest. Each one
            arrives with its website confirmed, and you're charged only for companies actually
            added.
          </p>
        </div>
      </div>

      <div className={styles.picker}>
        <div role="radiogroup" aria-label="How many companies" className={styles.switch}>
          {options.map((opt, i) => {
            const checked = opt === choice;
            return (
              <button
                key={String(opt)}
                ref={(el) => {
                  segmentRefs.current[i] = el;
                }}
                type="button"
                role="radio"
                aria-checked={checked}
                tabIndex={checked ? 0 : -1}
                className={cn(styles.segment, checked && styles.segmentOn)}
                onClick={() => {
                  setChoice(opt);
                  if (opt === "custom") setFocusCustom(true);
                }}
                onKeyDown={(e) => onSegmentKey(e, i)}
              >
                {opt === "custom" ? "Custom" : opt}
              </button>
            );
          })}
        </div>
        {choice === "custom" && (
          <div className={styles.custom}>
            <Input
              ref={customRef}
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX}
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder={`1 to ${MAX}`}
              aria-label="Number of companies"
              aria-invalid={custom !== "" && !countValid}
              aria-describedby="populate-cost"
            />
          </div>
        )}
      </div>

      <p id="populate-cost" className={styles.cost} aria-live="polite">
        {!countValid ? (
          choice === "custom" && custom !== "" ? (
            `Choose between 1 and ${MAX} companies.`
          ) : (
            "Enter how many companies to add."
          )
        ) : quote ? (
          <>
            <span className={styles.costStrong}>
              Up to {plural(quote.total_credits, "credit")}
            </span>{" "}
            for {plural(count, "company", "companies")} at {quote.credits_per_company} each
            {quote.balance != null && <>. You have {plural(Math.floor(quote.balance), "credit")}.</>}
          </>
        ) : quoteFailed ? (
          "Couldn't check the cost right now. You'll only be charged for companies added."
        ) : (
          <span className={styles.costPending}>Checking the cost</span>
        )}
      </p>

      {quote && !quote.enough && (
        <p className={cn(styles.inlineNote, styles.warning)}>
          <Icons.AlertTriangleIcon aria-hidden="true" />
          <span>
            {affordable != null && affordable > 0
              ? `Your balance covers ${plural(affordable, "company", "companies")}. `
              : "Your balance doesn't cover this. "}
            <Link to="/settings/billing">View plans and credits</Link>
          </span>
        </p>
      )}

      {refused && (
        <p className={cn(styles.inlineNote, styles.danger)} role="alert">
          <Icons.AlertTriangleIcon aria-hidden="true" />
          <span>
            {refused.text}{" "}
            {refused.billing && <Link to="/settings/billing">View plans and credits</Link>}
          </span>
        </p>
      )}

      <div className={styles.actions}>
        <Button
          onClick={start}
          loading={starting}
          // Not disabled when the quote says the balance is short: the server's check is the one
          // that decides (a plan in rollout may allow it), and it answers with a clear 402.
          disabled={!countValid}
          iconLeft={<Icons.PlusIcon />}
        >
          {countValid ? `Add ${plural(count, "company", "companies")}` : "Add companies"}
        </Button>
        <Button variant="ghost" onClick={dismiss} disabled={starting}>
          Not now
        </Button>
      </div>
    </Card>
  );
}
