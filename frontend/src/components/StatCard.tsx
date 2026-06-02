import type { ReactNode } from "react";
import { Card } from "@/components/ui";
import styles from "./StatCard.module.css";

export interface StatCardProps {
  label: string;
  value: ReactNode;
  icon?: ReactNode;
  /** Optional trailing hint (e.g. delta or unit). */
  hint?: ReactNode;
}

export function StatCard({ label, value, icon, hint }: StatCardProps) {
  return (
    <Card className={styles.card} padding="md">
      <div className={styles.row}>
        <span className={styles.label}>{label}</span>
        {icon && (
          <span className={styles.icon} aria-hidden="true">
            {icon}
          </span>
        )}
      </div>
      <div className={styles.value}>{value}</div>
      {hint && <div className={styles.hint}>{hint}</div>}
    </Card>
  );
}
