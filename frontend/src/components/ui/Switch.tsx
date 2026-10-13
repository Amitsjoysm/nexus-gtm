import { useId, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { CheckIcon } from "./icons";
import styles from "./Switch.module.css";

export interface SwitchProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** Accessible name when no visible label points at the switch. */
  label?: string;
  /** Ids of the visible label and description, when a row supplies them. */
  labelledBy?: string;
  describedBy?: string;
}

/** An on/off control for a setting that applies the moment it is flipped (no Save button). */
export function Switch({ checked, onChange, disabled, label, labelledBy, describedBy }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={labelledBy ? undefined : label}
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      disabled={disabled}
      className={cn(styles.switch, checked && styles.on)}
      onClick={() => onChange(!checked)}
    >
      <span className={styles.knob} aria-hidden="true">
        {checked && <CheckIcon />}
      </span>
    </button>
  );
}

export interface SwitchRowProps extends Omit<SwitchProps, "label" | "labelledBy" | "describedBy"> {
  label: ReactNode;
  hint?: ReactNode;
  /** A line under the hint saying what is in force, when that can differ from the switch. */
  status?: ReactNode;
}

/** A setting as a sentence: what it does on the left, the switch on the right, wired for screen
 *  readers so the switch is announced with its label and what it does. */
export function SwitchRow({ label, hint, status, ...rest }: SwitchRowProps) {
  const id = useId();
  return (
    <div className={styles.row}>
      <div className={styles.text}>
        <span id={`${id}-label`} className={styles.label}>{label}</span>
        {hint && <span id={`${id}-hint`} className={styles.hint}>{hint}</span>}
        {status && <span className={styles.status} role="status">{status}</span>}
      </div>
      <Switch {...rest} labelledBy={`${id}-label`} describedBy={hint ? `${id}-hint` : undefined} />
    </div>
  );
}
