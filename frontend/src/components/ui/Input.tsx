import { forwardRef } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";
import { useField } from "./Field";
import styles from "./control.module.css";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Decorative icon rendered inside the control, before the text. */
  iconLeft?: ReactNode;
  invalid?: boolean;
}

/** Text input. Inherits id/aria from a wrapping <Field>; standalone works too. */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { iconLeft, invalid, className, id, "aria-describedby": describedBy, ...rest },
  ref,
) {
  const field = useField();
  const isInvalid = invalid ?? field?.invalid ?? false;
  const control = (
    <input
      ref={ref}
      id={id ?? field?.id}
      aria-invalid={isInvalid || undefined}
      aria-describedby={describedBy ?? field?.describedBy}
      className={cn(styles.control, iconLeft && styles.hasIcon, isInvalid && styles.invalid, className)}
      {...rest}
    />
  );
  if (!iconLeft) return control;
  return (
    <div className={styles.wrap}>
      <span className={styles.leadIcon} aria-hidden="true">
        {iconLeft}
      </span>
      {control}
    </div>
  );
});
