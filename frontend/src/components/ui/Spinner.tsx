import { cn } from "@/lib/cn";
import styles from "./Spinner.module.css";

export interface SpinnerProps {
  /** Diameter in px. Default 18. */
  size?: number;
  /** Accessible label; rendered as sr-only text + aria-label. */
  label?: string;
  className?: string;
}

/** Indeterminate loading indicator. Inherits currentColor. */
export function Spinner({ size = 18, label = "Loading", className }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-live="polite"
      className={cn(styles.spinner, className)}
      style={{ width: size, height: size, borderWidth: Math.max(2, Math.round(size / 9)) }}
    >
      <span className="sr-only">{label}</span>
    </span>
  );
}
