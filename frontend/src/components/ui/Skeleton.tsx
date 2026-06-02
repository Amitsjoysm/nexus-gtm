import type { CSSProperties } from "react";
import { cn } from "@/lib/cn";
import styles from "./Skeleton.module.css";

export interface SkeletonProps {
  width?: number | string;
  height?: number | string;
  radius?: number | string;
  /** Render as a circle (e.g. avatar placeholder). */
  circle?: boolean;
  className?: string;
  style?: CSSProperties;
}

/**
 * Shimmer placeholder. Compose several to mirror the real layout while loading,
 * rather than showing a bare spinner for content areas.
 */
export function Skeleton({ width, height = 14, radius, circle, className, style }: SkeletonProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(styles.skeleton, className)}
      style={{
        width: circle ? height : width ?? "100%",
        height,
        borderRadius: circle ? "50%" : (radius ?? "var(--radius-sm)"),
        ...style,
      }}
    />
  );
}

/** Multiple stacked text lines; last line is shorter for realism. */
export function SkeletonText({ lines = 3, gap = 8 }: { lines?: number; gap?: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap }} aria-hidden="true">
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} height={12} width={i === lines - 1 ? "60%" : "100%"} />
      ))}
    </div>
  );
}
