import type { ReactNode } from "react";
import { Button } from "@/components/ui";
import { formatNumber } from "@/lib/format";
import styles from "./Prospector.module.css";

export interface BulkAction {
  key: string;
  label: string;
  icon?: ReactNode;
  primary?: boolean;
  onClick: () => void;
}

export interface BulkBarProps {
  /** Rows ticked on this page, or the select-all limit when `allMatching`. */
  count: number;
  allMatching: boolean;
  /** Offer "select all matching": the whole page is ticked and there is more. */
  canSelectAll: boolean;
  selectAllLimit: number;
  onSelectAll: () => void;
  onClear: () => void;
  actions: BulkAction[];
}

/**
 * The selection bar, in the Contacts page's own idiom (accent-quiet strip, count left, actions
 * right), sticky at the bottom of the viewport so the actions stay in reach while scrolling a long
 * page.
 */
export function BulkBar({
  count, allMatching, canSelectAll, selectAllLimit, onSelectAll, onClear, actions,
}: BulkBarProps) {
  return (
    <div className={styles.bulkBar} role="region" aria-label="Selection actions">
      <div className={styles.bulkCount}>
        <span>
          {allMatching
            ? `All matching selected (up to ${formatNumber(selectAllLimit)})`
            : `${formatNumber(count)} selected`}
        </span>
        {canSelectAll && !allMatching && (
          <button type="button" className={styles.textButton} onClick={onSelectAll}>
            Select all matching (up to {formatNumber(selectAllLimit)})
          </button>
        )}
        <button type="button" className={styles.textButton} onClick={onClear}>Clear</button>
      </div>
      <div className={styles.bulkActions}>
        {actions.map((a) => (
          <Button key={a.key} size="sm" variant={a.primary ? "primary" : "secondary"}
            iconLeft={a.icon} onClick={a.onClick}>
            {a.label}
          </Button>
        ))}
      </div>
    </div>
  );
}
