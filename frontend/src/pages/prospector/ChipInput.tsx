import { useId, useState } from "react";
import type { KeyboardEvent } from "react";
import { Icons } from "@/components/ui";
import { cn } from "@/lib/cn";
import { MAX_CHARS, MAX_VALUES } from "./filterState";
import styles from "./ChipInput.module.css";

export interface ChipInputProps {
  label: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  disabled?: boolean;
  /** An exclusion reads differently at a glance: its chips carry a minus. */
  exclude?: boolean;
}

/**
 * Several values for one filter, typed as chips: Enter or a comma adds, Backspace on an empty box
 * removes the last, each chip has its own remove button. Values are trimmed, capped at the server's
 * limits (50 values, 120 characters) and de-duplicated without regard to case, so the request the
 * rail builds is one the server accepts.
 */
export function ChipInput({ label, values, onChange, placeholder, disabled, exclude }: ChipInputProps) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const full = values.length >= MAX_VALUES;

  function commit(raw: string) {
    const parts = raw.split(",").map((p) => p.trim().replace(/\s+/g, " ").slice(0, MAX_CHARS))
      .filter(Boolean);
    if (!parts.length) return;
    const seen = new Set(values.map((v) => v.toLowerCase()));
    const next = [...values];
    for (const part of parts) {
      if (next.length >= MAX_VALUES) break;
      if (!seen.has(part.toLowerCase())) {
        seen.add(part.toLowerCase());
        next.push(part);
      }
    }
    onChange(next);
    setDraft("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commit(draft);
    } else if (e.key === "Backspace" && draft === "" && values.length) {
      onChange(values.slice(0, -1));
    }
  }

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
        />
      </div>
    </div>
  );
}
