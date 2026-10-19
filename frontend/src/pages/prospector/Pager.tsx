import { Button, Icons, Select } from "@/components/ui";
import styles from "./Prospector.module.css";

export interface PagerProps {
  /** What to call where we are: "Page 2", "Scanned 145". */
  position: string;
  canPrev: boolean;
  canNext: boolean;
  nextLabel?: string;
  onPrev: () => void;
  onNext: () => void;
  pageSize: number;
  onPageSize: (size: number) => void;
}

/** Prev / Next paging with the page size beside it. There is no total to page to unless asked. */
export function Pager({
  position, canPrev, canNext, nextLabel = "Next", onPrev, onNext, pageSize, onPageSize,
}: PagerProps) {
  return (
    <nav className={styles.pager} aria-label="Pages">
      <label className={styles.pageSize}>
        <span>Rows</span>
        <Select value={String(pageSize)} onChange={(e) => onPageSize(Number(e.target.value))}
          options={[25, 50, 100].map((n) => ({ value: String(n), label: String(n) }))}
          aria-label="Rows per page" />
      </label>
      <span className={styles.position} aria-live="polite">{position}</span>
      <div className={styles.pagerButtons}>
        <Button variant="secondary" size="sm" onClick={onPrev} disabled={!canPrev}
          iconLeft={<Icons.ChevronLeftIcon />}>
          Previous
        </Button>
        <Button variant="secondary" size="sm" onClick={onNext} disabled={!canNext}
          iconRight={<Icons.ChevronRightIcon />}>
          {nextLabel}
        </Button>
      </div>
    </nav>
  );
}
