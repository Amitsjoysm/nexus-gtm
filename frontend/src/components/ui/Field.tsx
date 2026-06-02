import { createContext, useContext, useId } from "react";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import styles from "./Field.module.css";

interface FieldContextValue {
  id: string;
  describedBy?: string;
  invalid: boolean;
}

const FieldContext = createContext<FieldContextValue | null>(null);

/** Inputs call this to wire id/aria-describedby/aria-invalid from their wrapping Field. */
export function useField() {
  return useContext(FieldContext);
}

export interface FieldProps {
  label: string;
  /** Helper text shown under the control. */
  hint?: ReactNode;
  /** Error message; when set, the control is marked invalid and described by it. */
  error?: ReactNode;
  /** Visually hide the label (still read by screen readers). */
  hideLabel?: boolean;
  required?: boolean;
  className?: string;
  children: ReactNode;
}

/**
 * Accessible form field: associates a <label>, hint, and error with its control.
 * Wrap a single Input/Textarea/Select; they pick up ids and aria via context.
 */
export function Field({
  label,
  hint,
  error,
  hideLabel,
  required,
  className,
  children,
}: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = cn(hint && hintId, error && errorId) || undefined;

  return (
    <FieldContext.Provider value={{ id, describedBy, invalid: !!error }}>
      <div className={cn(styles.field, className)}>
        <label htmlFor={id} className={cn(styles.label, hideLabel && "sr-only")}>
          {label}
          {required && (
            <span className={styles.required} aria-hidden="true">
              {" "}
              *
            </span>
          )}
        </label>
        {children}
        {hint && !error && (
          <p id={hintId} className={styles.hint}>
            {hint}
          </p>
        )}
        {error && (
          <p id={errorId} className={styles.error} role="alert">
            {error}
          </p>
        )}
      </div>
    </FieldContext.Provider>
  );
}
