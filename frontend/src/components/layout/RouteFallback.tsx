import { Spinner } from "@/components/ui";
import styles from "./RouteFallback.module.css";

/**
 * Suspense fallback for lazily loaded route chunks. Intentionally minimal: a
 * lazy chunk usually resolves in well under a frame on a warm cache, so we delay
 * the spinner (CSS animation-delay) to avoid a flash on fast transitions. When a
 * fetch genuinely takes time (cold cache, slow network) the spinner fades in.
 */
export function RouteFallback() {
  return (
    <div className={styles.root} role="status" aria-live="polite">
      <Spinner size={20} />
      <span className="sr-only">Loading…</span>
    </div>
  );
}
