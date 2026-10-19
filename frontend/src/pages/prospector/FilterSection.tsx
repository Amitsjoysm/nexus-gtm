import { useId } from "react";
import type { ReactNode } from "react";
import { Icons } from "@/components/ui";
import { cn } from "@/lib/cn";
import styles from "./FilterRail.module.css";

export interface FilterSectionProps {
  label: string;
  /** How many of its filters are set; shown on the header so a closed section still reports. */
  count: number;
  open: boolean;
  onToggle: () => void;
  onClear: () => void;
  /** Why the section cannot apply on this tab, when it cannot. */
  note?: string;
  children: ReactNode;
}

/**
 * One accordion section of the rail. The header is a real button with `aria-expanded` and
 * `aria-controls`, so it opens from the keyboard and announces its state; the count badge keeps a
 * closed section honest about what it is filtering.
 */
export function FilterSection({ label, count, open, onToggle, onClear, note, children }: FilterSectionProps) {
  const panelId = useId();
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead}>
        <button
          type="button"
          className={styles.toggle}
          aria-expanded={open}
          aria-controls={panelId}
          onClick={onToggle}
        >
          <Icons.ChevronDownIcon className={cn(styles.chevron, open && styles.chevronOpen)} />
          <span className={styles.sectionLabel}>{label}</span>
          {count > 0 && (
            <span className={styles.count} aria-label={`${count} active`}>{count}</span>
          )}
        </button>
        {count > 0 && (
          <button type="button" className={styles.clear} onClick={onClear}
            aria-label={`Clear ${label}`}>
            Clear
          </button>
        )}
      </div>
      {open && (
        <div id={panelId} className={styles.panel}>
          {note && (
            <p className={styles.note}>
              <Icons.LockIcon />
              <span>{note}</span>
            </p>
          )}
          {children}
        </div>
      )}
    </div>
  );
}
