import { forwardRef } from "react";
import type { SelectHTMLAttributes } from "react";
import { cn } from "@/lib/cn";
import { useField } from "./Field";
import styles from "./control.module.css";

export interface SelectOption {
  value: string;
  label: string;
  /** Listed but not choosable — e.g. a provider the product knows about but cannot connect yet. */
  disabled?: boolean;
  /** Optional heading this option belongs under. Options carrying the same group are rendered in
   *  one `<optgroup>`, in first-appearance order; ungrouped options stay at the top level.
   *  A real optgroup rather than a disabled separator row, because a separator is announced as an
   *  option a screen reader user then has to skip. */
  group?: string;
}

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  options: SelectOption[];
  placeholder?: string;
  invalid?: boolean;
}

/** Group headings in first-appearance order, so the caller controls the order by ordering options. */
function groupNames(options: SelectOption[]): string[] {
  const seen: string[] = [];
  for (const o of options) {
    if (o.group && !seen.includes(o.group)) seen.push(o.group);
  }
  return seen;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { options, placeholder, invalid, className, id, ...rest },
  ref,
) {
  const field = useField();
  const isInvalid = invalid ?? field?.invalid ?? false;
  return (
    <div className={styles.selectWrap}>
      <select
        ref={ref}
        id={id ?? field?.id}
        aria-invalid={isInvalid || undefined}
        aria-describedby={field?.describedBy}
        className={cn(styles.control, styles.select, isInvalid && styles.invalid, className)}
        {...rest}
      >
        {placeholder && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {options
          .filter((o) => !o.group)
          .map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
        {groupNames(options).map((name) => (
          <optgroup key={name} label={name}>
            {options
              .filter((o) => o.group === name)
              .map((o) => (
                <option key={o.value} value={o.value} disabled={o.disabled}>
                  {o.label}
                </option>
              ))}
          </optgroup>
        ))}
      </select>
      <span className={styles.caret} aria-hidden="true">
        ▾
      </span>
    </div>
  );
});
