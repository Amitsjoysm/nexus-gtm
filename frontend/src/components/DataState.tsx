import type { ReactNode } from "react";
import { ErrorState } from "@/components/ui";
import type { AsyncState } from "@/hooks/useApi";

export interface DataStateProps<T> {
  state: AsyncState<T>;
  /** Skeleton layout shown on first load (matches the real content shape). */
  skeleton: ReactNode;
  /** Render function for the success state. */
  children: (data: T) => ReactNode;
  /** Predicate + node for the empty state (e.g. empty array). */
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  errorTitle?: string;
}

/**
 * Enforces the four-state contract for every data view:
 * loading → skeleton, error → ErrorState (+retry), empty → empty slot, success → children.
 * Keeps showing existing data during background refetches to avoid layout flicker.
 */
export function DataState<T>({
  state,
  skeleton,
  children,
  isEmpty,
  empty,
  errorTitle,
}: DataStateProps<T>) {
  const { data, error, loading, refetch } = state;

  if (loading && data == null) return <>{skeleton}</>;

  if (error && data == null) {
    return <ErrorState title={errorTitle} message={error.detail} onRetry={refetch} />;
  }

  if (data != null) {
    if (isEmpty?.(data) && empty) return <>{empty}</>;
    return <>{children(data)}</>;
  }

  return <>{skeleton}</>;
}
