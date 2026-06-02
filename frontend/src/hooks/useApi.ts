import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";

export interface AsyncState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** Re-run the fetcher (e.g. for a Retry button or after a mutation). */
  refetch: () => void;
  /** Imperatively replace the data (optimistic updates). */
  setData: (updater: T | ((prev: T | null) => T)) => void;
}

/**
 * Data-fetching hook with first-class loading/error states and request cancellation.
 *
 * The fetcher receives an AbortSignal; in-flight requests are cancelled when deps change
 * or the component unmounts, preventing setState-after-unmount and race conditions.
 *
 * @param fetcher  async function (signal) => data
 * @param deps     dependency list controlling when to refetch
 */
export function useApi<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: ReadonlyArray<unknown> = [],
): AsyncState<T> {
  const [data, setDataState] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  // Keep the latest fetcher without making it a dependency.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError(null);

    fetcherRef.current(controller.signal)
      .then((result) => {
        if (active) {
          setDataState(result);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!active) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof ApiError ? err : new ApiError(0, String(err?.message ?? err)));
        setLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  const setData = useCallback((updater: T | ((prev: T | null) => T)) => {
    setDataState((prev) => (typeof updater === "function" ? (updater as (p: T | null) => T)(prev) : updater));
  }, []);

  return { data, error, loading, refetch, setData };
}
