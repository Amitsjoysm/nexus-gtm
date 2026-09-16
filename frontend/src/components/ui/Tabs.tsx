import { useId, useRef } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { cn } from "@/lib/cn";
import styles from "./Tabs.module.css";

export interface TabItem {
  value: string;
  label: string;
  count?: number;
}

export interface TabsProps {
  items: TabItem[];
  value: string;
  onChange: (value: string) => void;
  className?: string;
  "aria-label"?: string;
  /**
   * `underline` (default) sits above page content. `segmented` is a recessed track with one raised
   * segment, filling its container: the same vocabulary as the billing interval switch, for a
   * choice that should read as a pair of buttons rather than as sections of a page.
   */
  variant?: "underline" | "segmented";
  /**
   * Makes tab and panel ids predictable (`{idPrefix}-tab-{value}`, `{idPrefix}-panel-{value}`), so a
   * caller can give its panel the id `aria-controls` points at. Generated when omitted.
   */
  idPrefix?: string;
}

/** Accessible tablist with roving arrow-key navigation. Render the panel yourself. */
export function Tabs({
  items,
  value,
  onChange,
  className,
  variant = "underline",
  idPrefix,
  ...rest
}: TabsProps) {
  const generatedId = useId();
  const baseId = idPrefix ?? generatedId;
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = index;
    if (e.key === "ArrowRight") next = (index + 1) % items.length;
    else if (e.key === "ArrowLeft") next = (index - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    else return;
    e.preventDefault();
    onChange(items[next].value);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="tablist"
      aria-label={rest["aria-label"]}
      className={cn(styles.tabs, variant === "segmented" && styles.segmented, className)}
    >
      {items.map((item, i) => {
        const selected = item.value === value;
        return (
          <button
            key={item.value}
            ref={(el) => (refs.current[i] = el)}
            // A button's default type is submit, so a tablist placed inside a form submitted it.
            type="button"
            role="tab"
            id={`${baseId}-tab-${item.value}`}
            aria-selected={selected}
            aria-controls={`${baseId}-panel-${item.value}`}
            tabIndex={selected ? 0 : -1}
            className={cn(styles.tab, selected && styles.selected)}
            onClick={() => onChange(item.value)}
            onKeyDown={(e) => onKeyDown(e, i)}
          >
            {item.label}
            {item.count != null && <span className={styles.count}>{item.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** Panel wrapper that pairs with Tabs' aria wiring. */
export function TabPanel({
  id,
  active,
  children,
}: {
  id: string;
  active: boolean;
  children: ReactNode;
}) {
  if (!active) return null;
  return (
    <div role="tabpanel" id={id} tabIndex={0}>
      {children}
    </div>
  );
}
