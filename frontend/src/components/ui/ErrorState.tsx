import { Button } from "./Button";
import { cn } from "@/lib/cn";
import styles from "./EmptyState.module.css";
import { AlertTriangleIcon } from "./icons";

export interface ErrorStateProps {
  title?: string;
  message?: string;
  onRetry?: () => void;
  retrying?: boolean;
  className?: string;
  compact?: boolean;
}

/** Error state with the real message and a Retry. Pairs with useApi's error branch. */
export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
  retrying,
  className,
  compact,
}: ErrorStateProps) {
  return (
    <div className={cn(styles.empty, compact && styles.compact, className)} role="alert">
      <div className={cn(styles.icon)} aria-hidden="true" style={{ background: "var(--danger-quiet)", color: "var(--danger)" }}>
        <AlertTriangleIcon />
      </div>
      <h3 className={styles.title}>{title}</h3>
      {message && <p className={styles.desc}>{message}</p>}
      {onRetry && (
        <div className={styles.action}>
          <Button variant="secondary" size="sm" onClick={onRetry} loading={retrying}>
            Try again
          </Button>
        </div>
      )}
    </div>
  );
}
