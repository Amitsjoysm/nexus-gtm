import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Skeleton } from "./Skeleton";
import styles from "./DataTable.module.css";

export interface Column<T> {
  key: string;
  header: ReactNode;
  /** Cell renderer. Defaults to String(row[key]). */
  render?: (row: T) => ReactNode;
  align?: "left" | "right" | "center";
  /** Fixed/preferred width, e.g. "120px" or "20%". */
  width?: string;
  /** Hide below the md breakpoint to keep mobile readable. */
  hideOnMobile?: boolean;
}

export interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  getRowKey: (row: T) => string;
  /** Show shimmer rows instead of data. */
  loading?: boolean;
  skeletonRows?: number;
  /** Rendered (full-width) when not loading and rows is empty. */
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
  caption?: string;
  className?: string;
}

/**
 * Reusable, accessible table with built-in loading skeletons and an empty slot.
 * Rows become buttons (keyboard-activatable) when onRowClick is provided.
 */
export function DataTable<T>({
  columns,
  rows,
  getRowKey,
  loading,
  skeletonRows = 6,
  empty,
  onRowClick,
  caption,
  className,
}: DataTableProps<T>) {
  const showEmpty = !loading && rows.length === 0;

  return (
    <div className={cn(styles.scroll, className)}>
      <table className={styles.table}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                style={{ width: c.width, textAlign: c.align ?? "left" }}
                className={cn(c.hideOnMobile && styles.hideMobile)}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading &&
            Array.from({ length: skeletonRows }).map((_, i) => (
              <tr key={`sk-${i}`} className={styles.row}>
                {columns.map((c) => (
                  <td key={c.key} className={cn(c.hideOnMobile && styles.hideMobile)}>
                    <Skeleton height={12} width={c.align === "right" ? "40%" : "70%"} />
                  </td>
                ))}
              </tr>
            ))}

          {!loading &&
            rows.map((row) => {
              const clickable = !!onRowClick;
              return (
                <tr
                  key={getRowKey(row)}
                  className={cn(styles.row, clickable && styles.clickable)}
                  onClick={clickable ? () => onRowClick(row) : undefined}
                  tabIndex={clickable ? 0 : undefined}
                  onKeyDown={
                    clickable
                      ? (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            onRowClick(row);
                          }
                        }
                      : undefined
                  }
                >
                  {columns.map((c) => (
                    <td
                      key={c.key}
                      style={{ textAlign: c.align ?? "left" }}
                      className={cn(c.hideOnMobile && styles.hideMobile)}
                    >
                      {c.render ? c.render(row) : String((row as Record<string, unknown>)[c.key] ?? "—")}
                    </td>
                  ))}
                </tr>
              );
            })}
        </tbody>
      </table>

      {showEmpty && <div className={styles.empty}>{empty}</div>}
    </div>
  );
}
