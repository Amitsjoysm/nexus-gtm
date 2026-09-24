import { Button } from "@/components/ui";
import type { BestTimeSuggestion } from "@/lib/types";
import styles from "./BestTimeHint.module.css";

/**
 * "Suggest best time" for manual timing (spec §8, §18.5): the suggestion in words, and a button that
 * applies it. The clock is in the contact's own time, which is also how a step's set time is read,
 * so applying it needs no conversion for a step; moving one person's next step converts it first.
 */
export function BestTimeHint({ suggestion, onUse, current }: {
  suggestion?: BestTimeSuggestion;
  onUse: (clock: string) => void;
  /** The time already chosen, so the button is not offered for what is already set. */
  current?: string | null;
}) {
  if (!suggestion) return null;
  return (
    <p className={styles.hint}>
      <span>{suggestion.text}</span>
      {current !== suggestion.clock && (
        <Button size="sm" variant="ghost" type="button" onClick={() => onUse(suggestion.clock)}>
          Use {suggestion.clock}
        </Button>
      )}
    </p>
  );
}
