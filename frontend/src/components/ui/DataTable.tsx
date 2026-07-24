import { useMemo, useState, type ReactNode } from "react";
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
  /**
   * Make this column's header clickable to sort. Provide ``sortValue`` for columns whose cell is
   * rendered JSX (so we know what to sort on); a bare ``sortable: true`` falls back to row[key].
   */
  sortable?: boolean;
  sortValue?: (row: T) => string | number | null | undefined;
}

type SortDir = "asc" | "desc";
interface SortState {
  key: string;
  dir: SortDir;
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
  /**
   * Intrinsic minimum width for the table. Below this the wrapper scrolls horizontally instead of
   * crushing columns — so the table keeps its shape when a side panel narrows the container.
   * Accepts any CSS length (e.g. 840 → "840px", "60rem").
   */
  minWidth?: number | string;
  /** Row density. "compact" tightens cell padding for column-heavy tables. */
  density?: "default" | "compact";
}

function isSortable<T>(c: Column<T>): boolean {
  return c.sortable === true || typeof c.sortValue === "function";
}

function sortAccessor<T>(c: Column<T>): (row: T) => string | number | null | undefined {
  if (c.sortValue) return c.sortValue;
  return (row) => (row as Record<string, unknown>)[c.key] as string | number | null | undefined;
}

function compareValues(
  a: string | number | null | undefined,
  b: string | number | null | undefined,
): number {
  // Blanks always sort to the bottom, regardless of direction.
  const aEmpty = a == null || a === "";
  const bEmpty = b == null || b === "";
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;
  if (bEmpty) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/**
 * Reusable, accessible table with built-in loading skeletons, an empty slot, and optional
 * per-column sorting. Rows become buttons (keyboard-activatable) when onRowClick is provided.
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
  minWidth,
  density = "default",
}: DataTableProps<T>) {
  const [sort, setSort] = useState<SortState | null>(null);
  const showEmpty = !loading && rows.length === 0;
  const tableMinWidth = typeof minWidth === "number" ? `${minWidth}px` : minWidth;

  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col || !isSortable(col)) return rows;
    const accessor = sortAccessor(col);
    const factor = sort.dir === "asc" ? 1 : -1;
    // Stable sort over a copy; blanks stay last in either direction.
    return [...rows].sort((ra, rb) => {
      const cmp = compareValues(accessor(ra), accessor(rb));
      return cmp === 0 ? 0 : cmp * factor;
    });
  }, [rows, sort, columns]);

  function toggleSort(key: string) {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: "asc" };
      if (prev.dir === "asc") return { key, dir: "desc" };
      return null; // third click clears sorting -> back to the incoming order
    });
  }

  return (
    <div className={cn(styles.scroll, className)}>
      <table
        className={cn(styles.table, density === "compact" && styles.compact)}
        style={tableMinWidth ? { minWidth: tableMinWidth } : undefined}
      >
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => {
              const sortable = isSortable(c);
              const active = sort?.key === c.key;
              const ariaSort = active ? (sort!.dir === "asc" ? "ascending" : "descending") : "none";
              return (
                <th
                  key={c.key}
                  scope="col"
                  aria-sort={sortable ? ariaSort : undefined}
                  style={{ width: c.width, textAlign: c.align ?? "left" }}
                  className={cn(c.hideOnMobile && styles.hideMobile)}
                >
                  {sortable ? (
                    <button
                      type="button"
                      className={cn(
                        styles.sortBtn,
                        c.align === "right" && styles.sortBtnRight,
                        active && styles.sortActive,
                      )}
                      onClick={() => toggleSort(c.key)}
                    >
                      <span>{c.header}</span>
                      <span aria-hidden="true" className={styles.sortIcon}>
                        {active ? (sort!.dir === "asc" ? "▲" : "▼") : "↕"}
                      </span>
                    </button>
                  ) : (
                    c.header
                  )}
                </th>
              );
            })}
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
            sortedRows.map((row) => {
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
