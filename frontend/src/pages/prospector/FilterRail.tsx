import { useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import {
  Badge, Button, Field, IconButton, Icons, Input, Select, SwitchRow,
} from "@/components/ui";
import type { ProspectList } from "@/lib/types";
import type {
  FilterCatalog,
  FilterFieldSpec,
  FilterSectionSpec,
  FilterValue,
  FilterValues,
  ProspectTab,
} from "@/lib/prospectorTypes";
import { useApiClient } from "@/app/AuthContext";
import { ChipInput } from "./ChipInput";
import { FilterSection } from "./FilterSection";
import { OptionList } from "./OptionList";
import { activeCount, isFilled, sectionCount, usableOn } from "./filterState";
import styles from "./FilterRail.module.css";

const OPTION_LABELS: Record<string, string> = {
  valid: "Valid", catch_all: "Catch-all", risky: "Risky", unknown: "Unknown", invalid: "Invalid",
  none: "Not checked",
};

function humanise(value: string): string {
  if (OPTION_LABELS[value]) return OPTION_LABELS[value];
  const text = value.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function noteFor(section: FilterSectionSpec, tab: ProspectTab): string | undefined {
  if (tab !== "saved" && section.workspace_only) {
    return "Works on the Saved tab, against records you already have.";
  }
  const unusable = section.fields.filter((f) => !usableOn(f, section, tab));
  if (!unusable.length) return undefined;
  const names = unusable.map((f) => f.label.toLowerCase()).join(", ");
  return tab === "saved"
    ? `Not stored for workspace records: ${names}.`
    : `Not searchable at the data provider yet: ${names}. These work on the Saved tab.`;
}

export interface FilterRailProps {
  catalog: FilterCatalog | null;
  /** The rail's own values: what the next Search will run, not necessarily what is shown. */
  values: FilterValues;
  tab: ProspectTab;
  lists: ProspectList[];
  /** The rail differs from the search the results show. */
  dirty: boolean;
  onChange: (key: string, value: FilterValue) => void;
  onClearAll: () => void;
  onClearKeys: (keys: string[]) => void;
  /** Run the search the rail describes. */
  onSearch: () => void;
  /** Put the rail back to the search the results show. */
  onReset: () => void;
  onHide: () => void;
}

/**
 * The filter rail: every section the server's catalog lists for this view, in its order, each an
 * accordion with its active count. A filter that cannot apply on the current tab stays visible but
 * disabled, with the reason, so switching tabs never hides what is set.
 *
 * Nothing runs until Search (2026-11-18). Every change used to start a paid search 800 ms after
 * the last keystroke, so building "CEO, CTO in fintech, 51-200" ran three searches on the way and
 * showed whichever filter landed last. The rail is a form: Search (or Enter in a text box, or
 * Ctrl+Enter anywhere in it) runs the whole set at once; Enter in a chip box adds the chip.
 */
export function FilterRail({
  catalog, values, tab, lists, dirty, onChange, onClearAll, onClearKeys, onSearch, onReset,
  onHide,
}: FilterRailProps) {
  const sections = catalog?.sections ?? [];
  const [open, setOpen] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    for (const s of sections) if (sectionCount(s, values) > 0) initial.add(s.key);
    if (!initial.size && sections[0]) initial.add(sections[0].key);
    return initial;
  });
  const total = activeCount(catalog, values);
  const api = useApiClient();

  /** Typeahead for a field that offers it: the provider's own values first (2026-11-19). */
  function suggestFor(field: FilterFieldSpec) {
    if (!field.suggest || !catalog) return undefined;
    const view = catalog.view;
    return (q: string, signal: AbortSignal) =>
      api.prospectorSuggest(view, field.key, q, signal).then((r) => r.values);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    onSearch();
  }

  function onKeyDown(e: KeyboardEvent<HTMLFormElement>) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      // A chip still being typed is part of what the person asked for: blurring its box commits
      // it, and the search runs after that render (the page reads the rail's latest values).
      (document.activeElement as HTMLElement | null)?.blur();
      window.setTimeout(onSearch, 0);
    }
  }

  function toggle(key: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function renderField(field: FilterFieldSpec, disabled: boolean) {
    const value = values[field.key];
    const list = Array.isArray(value) ? value : [];
    switch (field.kind) {
      case "text":
        return (
          <Field key={field.key} label={field.label}>
            <Input
              value={typeof value === "string" ? value : ""}
              onChange={(e) => onChange(field.key, e.target.value)}
              maxLength={120}
              disabled={disabled}
            />
          </Field>
        );
      case "bool":
        return (
          <SwitchRow
            key={field.key}
            label={field.label}
            checked={value === true}
            onChange={(next) => onChange(field.key, next ? true : null)}
            disabled={disabled}
          />
        );
      case "window":
        return (
          <Field key={field.key} label={field.label}>
            <Select
              value={String(typeof value === "number" ? value : 90)}
              onChange={(e) => onChange(field.key, Number(e.target.value))}
              disabled={disabled}
              options={field.options.map((o) => ({ value: o, label: `Last ${o} days` }))}
            />
          </Field>
        );
      case "id_list":
        return lists.length ? (
          <OptionList
            key={field.key}
            label={field.label}
            options={lists.map((l) => ({ value: l.id, label: l.name }))}
            values={list}
            onChange={(next) => onChange(field.key, next)}
            disabled={disabled}
          />
        ) : (
          <p key={field.key} className={styles.empty}>No lists yet.</p>
        );
      case "enum_list":
        if (field.options.length) {
          return (
            <OptionList
              key={field.key}
              label={field.label}
              options={field.options.map((o) => ({ value: o, label: humanise(o) }))}
              values={list}
              onChange={(next) => onChange(field.key, next)}
              disabled={disabled}
            />
          );
        }
        // An unobserved vocabulary is typed rather than guessed.
        return (
          <ChipInput key={field.key} label={field.label} values={list}
            onChange={(next) => onChange(field.key, next)} disabled={disabled}
            placeholder="Type and press Enter" suggest={suggestFor(field)}
            max={field.max_values} />
        );
      default:
        return (
          <ChipInput
            key={field.key}
            label={field.label}
            values={list}
            onChange={(next) => onChange(field.key, next)}
            disabled={disabled}
            exclude={field.key.startsWith("exclude_")}
            placeholder={field.suggest ? "Type 2 letters for suggestions" : "Type and press Enter"}
            suggest={suggestFor(field)}
            max={field.max_values}
          />
        );
    }
  }

  return (
    <aside className={styles.rail} aria-label="Filters">
      <form className={styles.form} onSubmit={submit} onKeyDown={onKeyDown} noValidate>
        <div className={styles.head}>
          <h2 className={styles.title}>Filters</h2>
          {total > 0 && <Badge tone="accent">{total}</Badge>}
          <Button variant="ghost" size="sm" onClick={onClearAll} disabled={total === 0}
            className={styles.clearAll}>
            Clear all
          </Button>
          <IconButton label="Hide filters" size="sm" icon={<Icons.PanelLeftCloseIcon />}
            onClick={onHide} />
        </div>
        {/* Search sits at the top, under the title, so it is on screen before the page is
            scrolled as well as while the rail is sticky; at the foot it was below the fold of a
            768px screen until the page moved. Never disabled: a chip typed but not yet entered
            joins the filters only when its box loses focus, which is the press of this button,
            and a disabled one would swallow that click. With nothing changed it searches again. */}
        <div className={styles.searchRow}>
          <Button type="submit" size="sm" iconLeft={<Icons.SearchIcon />}
            variant={dirty ? "primary" : "secondary"} className={styles.searchButton}>
            Search
          </Button>
          {dirty && (
            <Button type="button" variant="ghost" size="sm" onClick={onReset}>Reset</Button>
          )}
        </div>
        <p className={styles.status} aria-live="polite">
          {dirty ? "Changes not searched yet." : total ? "The results show these filters."
            : "Set filters, then Search."}
        </p>
        <div className={styles.sections}>
          {sections.map((section) => {
            const note = noteFor(section, tab);
            return (
              <FilterSection
                key={section.key}
                label={section.label}
                count={sectionCount(section, values)}
                open={open.has(section.key)}
                onToggle={() => toggle(section.key)}
                onClear={() => onClearKeys(section.fields.map((f) => f.key)
                  .filter((k) => isFilled(values[k])))}
                note={note}
              >
                <div className={styles.fields}>
                  {section.fields.map((f) => renderField(f, !usableOn(f, section, tab)))}
                </div>
                {section.hint && <p className={styles.hint}>{section.hint}</p>}
              </FilterSection>
            );
          })}
        </div>
      </form>
    </aside>
  );
}
