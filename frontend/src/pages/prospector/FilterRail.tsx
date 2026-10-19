import { useState } from "react";
import { Badge, Button, Field, Input, Select, SwitchRow } from "@/components/ui";
import type { ProspectList } from "@/lib/types";
import type {
  FilterCatalog,
  FilterFieldSpec,
  FilterSectionSpec,
  FilterValue,
  FilterValues,
  ProspectTab,
} from "@/lib/prospectorTypes";
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
  values: FilterValues;
  tab: ProspectTab;
  lists: ProspectList[];
  onChange: (key: string, value: FilterValue) => void;
  onClearAll: () => void;
  onClearKeys: (keys: string[]) => void;
}

/**
 * The filter rail: every section the server's catalog lists for this view, in its order, each an
 * accordion with its active count. A filter that cannot apply on the current tab stays visible but
 * disabled, with the reason, so switching tabs never hides what is set.
 */
export function FilterRail({
  catalog, values, tab, lists, onChange, onClearAll, onClearKeys,
}: FilterRailProps) {
  const sections = catalog?.sections ?? [];
  const [open, setOpen] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    for (const s of sections) if (sectionCount(s, values) > 0) initial.add(s.key);
    if (!initial.size && sections[0]) initial.add(sections[0].key);
    return initial;
  });
  const total = activeCount(catalog, values);

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
            placeholder="Type and press Enter" />
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
            placeholder="Type and press Enter"
          />
        );
    }
  }

  return (
    <aside className={styles.rail} aria-label="Filters">
      <div className={styles.head}>
        <h2 className={styles.title}>Filters</h2>
        {total > 0 && <Badge tone="accent">{total}</Badge>}
        <Button variant="ghost" size="sm" onClick={onClearAll} disabled={total === 0}
          className={styles.clearAll}>
          Clear all
        </Button>
      </div>
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
    </aside>
  );
}
