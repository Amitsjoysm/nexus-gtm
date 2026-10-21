import { Field, Select } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ProspectList } from "@/lib/types";

/**
 * Choose a saved list as a campaign's audience. The server expands it (a contact list to its
 * people, an account list to everyone with an email at its companies) and records it as the
 * campaign's source, so this only picks one and says, before anything is added, who that will be.
 */

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** Who choosing this list adds, in a sentence. */
export function audienceOf(list: ProspectList): string {
  return list.kind === "contact"
    ? `The ${plural(list.members, "person", "people")} on “${list.name}” who ${list.members === 1 ? "has" : "have"} an email address`
    : `Everyone with an email address at the ${plural(list.accounts, "company", "companies")} on “${list.name}”`;
}

function optionLabel(list: ProspectList): string {
  return list.kind === "contact"
    ? `${list.name} (${plural(list.members, "person", "people")})`
    : `${list.name} (${plural(list.accounts, "company", "companies")})`;
}

export interface ListChooserProps {
  value: string;
  onChange: (listId: string) => void;
  label: string;
  /** The first option, meaning "no list". */
  emptyLabel: string;
  /** Shown while no list is chosen; once one is, the field says who it adds. */
  hint?: string;
  hideLabel?: boolean;
}

export function ListChooser({ value, onChange, label, emptyLabel, hint, hideLabel }: ListChooserProps) {
  const api = useApiClient();
  const lists = useApi<ProspectList[]>((s) => api.listSavedLists(s), []);
  const all = lists.data ?? [];
  const chosen = all.find((l) => l.id === value);
  const options = [
    { value: "", label: emptyLabel },
    ...all.map((l) => ({ value: l.id, label: optionLabel(l), disabled: l.members === 0 })),
  ];

  return (
    <Field
      label={label}
      hideLabel={hideLabel}
      hint={chosen ? `${audienceOf(chosen)}.` : all.length === 0 && lists.data ? "No saved lists yet. Make one in Lists." : hint}
      error={lists.error ? "Your lists couldn't be loaded. Try again in a moment." : undefined}
    >
      <Select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        options={options}
        disabled={lists.loading && !lists.data}
      />
    </Field>
  );
}
