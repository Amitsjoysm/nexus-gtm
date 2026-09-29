import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import type { InputHTMLAttributes } from "react";
import { cn } from "@/lib/cn";
import styles from "./Checkbox.module.css";

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  /** Some but not all of what this box stands for is ticked (a "select all" header). */
  indeterminate?: boolean;
  /** Read by screen readers; a table checkbox has no visible label. */
  label: string;
}

/**
 * A native checkbox with an indeterminate state and a hit area larger than the box itself, so a
 * row of them in a dense table is still easy to tick. The click never reaches the row, which in
 * these tables opens the record.
 */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { indeterminate = false, label, className, checked, onClick, ...rest },
  forwarded,
) {
  const ref = useRef<HTMLInputElement>(null);
  useImperativeHandle(forwarded, () => ref.current as HTMLInputElement);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate && !checked;
  }, [indeterminate, checked]);

  return (
    <span className={cn(styles.hit, className)} onClick={(e) => e.stopPropagation()}>
      <input
        ref={ref}
        type="checkbox"
        className={styles.box}
        checked={checked}
        aria-label={label}
        onClick={onClick}
        {...rest}
      />
    </span>
  );
});
