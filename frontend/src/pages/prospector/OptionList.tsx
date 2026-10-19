import { useId } from "react";
import styles from "./OptionList.module.css";

export interface OptionListProps {
  label: string;
  options: { value: string; label: string }[];
  values: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

/** A small fixed vocabulary (seniority, size bands, verification) as a group of checkboxes. */
export function OptionList({ label, options, values, onChange, disabled }: OptionListProps) {
  const id = useId();
  const chosen = new Set(values);

  function toggle(value: string) {
    onChange(chosen.has(value) ? values.filter((v) => v !== value) : [...values, value]);
  }

  return (
    <fieldset className={styles.group} disabled={disabled}>
      <legend className={styles.legend}>{label}</legend>
      <div className={styles.options}>
        {options.map((option) => {
          const optionId = `${id}-${option.value}`;
          return (
            <label key={option.value} className={styles.option} htmlFor={optionId}>
              <input
                id={optionId}
                type="checkbox"
                className={styles.box}
                checked={chosen.has(option.value)}
                onChange={() => toggle(option.value)}
              />
              <span>{option.label}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
