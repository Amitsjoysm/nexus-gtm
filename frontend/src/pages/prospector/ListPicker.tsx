import { useEffect, useMemo } from "react";
import { Field, Input, Select, Skeleton } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { useApi } from "@/hooks/useApi";
import { formatNumber } from "@/lib/format";
import type { ListKind, ProspectList } from "@/lib/types";
import styles from "./Prospector.module.css";

export interface ListChoice {
  mode: "existing" | "new";
  listId: string;
  name: string;
}

export function listChoiceReady(choice: ListChoice): boolean {
  return choice.mode === "existing" ? choice.listId !== "" : choice.name.trim() !== "";
}

/**
 * Which list a delivery goes on: one the person can change, of the kind the action needs, or a new
 * one named here. Reads the same endpoint as `AddToListModal`, which stays unchanged.
 */
export function ListPicker({ kind, value, onChange }: {
  kind: ListKind;
  value: ListChoice;
  onChange: (next: ListChoice) => void;
}) {
  const api = useApiClient();
  const lists = useApi<ProspectList[]>((signal) => api.listSavedLists(signal, kind), [kind]);
  const mine = useMemo(() => (lists.data ?? []).filter((l) => l.can_edit), [lists.data]);

  useEffect(() => {
    if (lists.loading) return;
    if (!mine.length && value.mode === "existing") onChange({ ...value, mode: "new" });
    else if (value.mode === "existing" && !value.listId && mine[0]) {
      onChange({ ...value, listId: mine[0].id });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lists.loading, mine]);

  if (lists.loading && !lists.data) return <Skeleton height={72} />;
  const noun = kind === "account" ? "account list" : "contact list";

  return (
    <fieldset className={styles.listPicker}>
      <legend className={styles.legend}>Which {noun}</legend>
      <div className={styles.choices}>
        <label className={styles.choice}>
          <input type="radio" name="prospector-list-mode" checked={value.mode === "existing"}
            disabled={!mine.length} onChange={() => onChange({ ...value, mode: "existing" })} />
          <span>An existing list{!mine.length && <span className={styles.secondary}> (none yet)</span>}</span>
        </label>
        <label className={styles.choice}>
          <input type="radio" name="prospector-list-mode" checked={value.mode === "new"}
            onChange={() => onChange({ ...value, mode: "new" })} />
          <span>A new list</span>
        </label>
      </div>
      {value.mode === "existing" ? (
        <Field label="List">
          <Select value={value.listId} onChange={(e) => onChange({ ...value, listId: e.target.value })}
            options={mine.map((l) => ({ value: l.id,
              label: `${l.name} (${formatNumber(l.members)})` }))} />
        </Field>
      ) : (
        <Field label="List name" required>
          <Input value={value.name} maxLength={200}
            placeholder={kind === "account" ? "Cold accounts to track" : "Cold outreach, Q4"}
            onChange={(e) => onChange({ ...value, name: e.target.value })} />
        </Field>
      )}
    </fieldset>
  );
}
