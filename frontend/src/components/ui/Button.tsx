import { forwardRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Spinner } from "./Spinner";
import styles from "./Button.module.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "subtle";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner, disables interaction, and announces busy state. */
  loading?: boolean;
  /** Stretch to fill the container width. */
  fullWidth?: boolean;
  /** Optional leading/trailing icons (decorative). */
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
}

/**
 * Primary action button. Accessible by default: real <button>, disabled+busy when loading,
 * visible focus ring from global styles, ≥44px target at md/lg.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "primary",
    size = "md",
    loading = false,
    fullWidth = false,
    iconLeft,
    iconRight,
    disabled,
    type = "button",
    className,
    children,
    ...rest
  },
  ref,
) {
  const isDisabled = disabled || loading;
  return (
    <button
      ref={ref}
      type={type}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      data-loading={loading || undefined}
      className={cn(
        styles.button,
        styles[variant],
        styles[size],
        fullWidth && styles.fullWidth,
        className,
      )}
      {...rest}
    >
      {loading && <Spinner size={size === "lg" ? 18 : 15} className={styles.spinner} />}
      {!loading && iconLeft && <span className={styles.icon} aria-hidden="true">{iconLeft}</span>}
      {children != null && <span className={styles.label}>{children}</span>}
      {!loading && iconRight && <span className={styles.icon} aria-hidden="true">{iconRight}</span>}
    </button>
  );
});
