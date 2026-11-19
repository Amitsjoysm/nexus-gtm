import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Icons } from "@/components/ui";
import { cn } from "@/lib/cn";
import { MAX_CHARS, MAX_VALUES } from "./filterState";
import styles from "./ChipInput.module.css";

/** Suggestions start at this many typed characters, after a short pause in typing. */
export const SUGGEST_MIN_CHARS = 2;
const SUGGEST_DELAY_MS = 180;

export interface ChipInputProps {
  label: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  disabled?: boolean;
  /** An exclusion reads differently at a glance: its chips carry a minus. */
  exclude?: boolean;
  /** The most chips this filter takes (job titles: the provider is asked once for each). */
  max?: number | null;
  /** Values to offer as the person types: the provider's own spellings first. */
  suggest?: (query: string, signal: AbortSignal) => Promise<string[]>;
}

function Highlighted({ text, query }: { text: string; query: string }) {
  const at = text.toLowerCase().indexOf(query.toLowerCase());
  if (!query || at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark className={styles.match}>{text.slice(at, at + query.length)}</mark>
      {text.slice(at + query.length)}
    </>
  );
}

/**
 * Several values for one filter, typed as chips: Enter or a comma adds, Backspace on an empty box
 * removes the last, each chip has its own remove button. Values are trimmed, capped at the server's
 * limits (50 values, 120 characters) and de-duplicated without regard to case, so the request the
 * rail builds is one the server accepts.
 *
 * With `suggest`, it is a combobox (2026-11-19): from two characters it lists values the provider's
 * own data holds, arrow keys move through them, Enter or a click takes one, Escape closes the list.
 * The list sits in the flow under the box rather than floating, so the rail's own scroller never
 * clips it.
 */
export function ChipInput({
  label, values, onChange, placeholder, disabled, exclude, max, suggest,
}: ChipInputProps) {
  const id = useId();
  const listId = `${id}-suggestions`;
  const [draft, setDraft] = useState("");
  const [options, setOptions] = useState<string[]>([]);
  const [active, setActive] = useState(-1);
  const [open, setOpen] = useState(false);
  const abort = useRef<AbortController | null>(null);
  // Read through refs: the rail hands a new function (and often a new array) on every render, and
  // only the typed text should start a lookup.
  const suggestRef = useRef(suggest);
  suggestRef.current = suggest;
  const valuesRef = useRef(values);
  valuesRef.current = values;
  const canSuggest = Boolean(suggest);
  const limit = Math.min(max ?? MAX_VALUES, MAX_VALUES);
  const full = values.length >= limit;
  const query = draft.trim();

  useEffect(() => {
    if (!canSuggest || query.length < SUGGEST_MIN_CHARS || full) {
      setOptions([]);
      setOpen(false);
      return undefined;
    }
    const timer = window.setTimeout(() => {
      const lookup = suggestRef.current;
      if (!lookup) return;
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      lookup(query, controller.signal)
        .then((found) => {
          if (controller.signal.aborted) return;
          const chosen = new Set(valuesRef.current.map((v) => v.toLowerCase()));
          const fresh = found.filter((v) => !chosen.has(v.toLowerCase()));
          setOptions(fresh);
          setActive(-1);
          setOpen(fresh.length > 0);
        })
        .catch(() => {
          // A suggestion that fails costs the list, never the typing.
          if (!controller.signal.aborted) setOpen(false);
        });
    }, SUGGEST_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [canSuggest, query, full]);

  useEffect(() => () => abort.current?.abort(), []);

  function commit(raw: string) {
    const parts = raw.split(",").map((p) => p.trim().replace(/\s+/g, " ").slice(0, MAX_CHARS))
      .filter(Boolean);
    setOpen(false);
    if (!parts.length) return;
    const seen = new Set(values.map((v) => v.toLowerCase()));
    const next = [...values];
    for (const part of parts) {
      if (next.length >= limit) break;
      if (!seen.has(part.toLowerCase())) {
        seen.add(part.toLowerCase());
        next.push(part);
      }
    }
    onChange(next);
    setDraft("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (open && options.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (i + step + options.length) % options.length);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "Enter" || e.key === ",") {
      // Ctrl+Enter belongs to the rail (Search); plain Enter adds a chip.
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      commit(open && active >= 0 ? options[active] : draft);
    } else if (e.key === "Backspace" && draft === "" && values.length) {
      onChange(values.slice(0, -1));
    }
  }

  const optionId = (i: number) => `${id}-option-${i}`;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>{label}</label>
      <div className={cn(styles.box, disabled && styles.disabled)}>
        {values.map((value) => (
          <span key={value} className={cn(styles.chip, exclude && styles.exclude)}>
            {exclude && <Icons.MinusIcon className={styles.minus} />}
            <span className={styles.chipText}>{value}</span>
            <button
              type="button"
              className={styles.remove}
              onClick={() => onChange(values.filter((v) => v !== value))}
              disabled={disabled}
              aria-label={`Remove ${value} from ${label}`}
            >
              <Icons.XIcon />
            </button>
          </span>
        ))}
        <input
          id={id}
          className={styles.input}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          onBlur={() => commit(draft)}
          placeholder={values.length ? "" : placeholder}
          disabled={disabled || full}
          maxLength={MAX_CHARS}
          autoComplete="off"
          {...(suggest ? {
            role: "combobox",
            "aria-autocomplete": "list" as const,
            "aria-expanded": open,
            "aria-controls": listId,
            "aria-activedescendant": open && active >= 0 ? optionId(active) : undefined,
          } : {})}
        />
      </div>
      {suggest && open && (
        <ul id={listId} role="listbox" aria-label={`${label} suggestions`}
          className={styles.suggestions}>
          {options.map((option, i) => (
            <li
              key={option}
              id={optionId(i)}
              role="option"
              aria-selected={i === active}
              className={cn(styles.option, i === active && styles.optionActive)}
              // Before the input's blur, so the click lands on the option, not on the typed text.
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => commit(option)}
            >
              <Highlighted text={option} query={query} />
            </li>
          ))}
        </ul>
      )}
      {max && values.length >= max && (
        <p className={styles.limit}>Up to {max} at once.</p>
      )}
    </div>
  );
}
