import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import styles from "./EmptyState.module.css";

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  /** Primary action (and optional secondary) — give users a way forward. */
  action?: ReactNode;
  className?: string;
  /** Compact variant for inline/within-card emptiness. */
  compact?: boolean;
}

/** Friendly empty state: icon, headline, one-line guidance, and a way forward. */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  compact,
}: EmptyStateProps) {
  return (
    <div className={cn(styles.empty, compact && styles.compact, className)} role="status">
      {icon && (
        <div className={styles.icon} aria-hidden="true">
          {icon}
        </div>
      )}
      <h3 className={styles.title}>{title}</h3>
      {description && <p className={styles.desc}>{description}</p>}
      {action && <div className={styles.action}>{action}</div>}
    </div>
  );
}
