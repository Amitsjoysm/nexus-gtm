import { cn } from "@/lib/cn";
import styles from "./Spinner.module.css";

export interface SpinnerProps {
  /** Diameter in px. Default 18. */
  size?: number;
  /** Accessible label; rendered as sr-only text + aria-label. */
  label?: string;
  /** Visual only: no live region, hidden from assistive tech. For a spinner inside something that
   *  is already the announcement, such as `WorkingIndicator`, where a second live region would read
   *  "Loading" on top of the sentence that says what is loading. */
  decorative?: boolean;
  className?: string;
}

/** Indeterminate loading indicator. Inherits currentColor. */
export function Spinner({
  size = 18,
  label = "Loading",
  decorative = false,
  className,
}: SpinnerProps) {
  const style = { width: size, height: size, borderWidth: Math.max(2, Math.round(size / 9)) };
  if (decorative) {
    return <span aria-hidden="true" className={cn(styles.spinner, className)} style={style} />;
  }
  return (
    <span role="status" aria-live="polite" className={cn(styles.spinner, className)} style={style}>
      <span className="sr-only">{label}</span>
    </span>
  );
}
