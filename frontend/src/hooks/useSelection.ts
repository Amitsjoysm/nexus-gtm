import { useCallback, useMemo, useState } from "react";

/**
 * Ticked rows in a table, kept by id so a selection survives filtering and paging. The page's
 * "select all" acts on the ids shown, never on everything that exists: ticking a header box must
 * not quietly add rows the person cannot see.
 */
export function useSelection(visibleIds: string[]) {
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const toggle = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const allVisible = visibleIds.length > 0 && visibleIds.every((id) => selected.has(id));
  const someVisible = visibleIds.some((id) => selected.has(id));

  const toggleVisible = useCallback(() => {
    setSelected((prev) => {
      const next = new Set(prev);
      const every = visibleIds.length > 0 && visibleIds.every((id) => prev.has(id));
      for (const id of visibleIds) {
        if (every) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  }, [visibleIds]);

  const clear = useCallback(() => setSelected(new Set()), []);
  const ids = useMemo(() => [...selected], [selected]);

  return { selected, ids, toggle, toggleVisible, clear, allVisible, someVisible };
}
