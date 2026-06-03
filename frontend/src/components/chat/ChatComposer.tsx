import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";
import { Button, Icons } from "@/components/ui";
import styles from "./ChatComposer.module.css";

export interface ChatComposerProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  /** A send is in flight: disables the control and shows a busy affordance. */
  busy?: boolean;
  placeholder?: string;
}

const MAX_ROWS = 6;

/**
 * Message input for the orchestrator. Enter sends; Shift+Enter inserts a newline. The
 * textarea auto-grows up to ~6 rows, then scrolls. Empty/whitespace-only input is ignored.
 */
export function ChatComposer({
  onSend,
  disabled = false,
  busy = false,
  placeholder = "Message the orchestrator…",
}: ChatComposerProps) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  // Auto-grow: reset to content height, capped at MAX_ROWS via max-height in CSS.
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${node.scrollHeight}px`;
  }, [value]);

  const canSend = value.trim() !== "" && !disabled && !busy;

  const submit = useCallback(() => {
    const text = value.trim();
    if (text === "" || disabled || busy) return;
    onSend(text);
    setValue("");
  }, [value, disabled, busy, onSend]);

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function handleChange(event: ChangeEvent<HTMLTextAreaElement>) {
    setValue(event.target.value);
  }

  return (
    <form
      className={styles.composer}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={ref}
        className={styles.input}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={1}
        aria-label="Message the orchestrator"
        disabled={disabled}
        style={{ maxHeight: `calc(${MAX_ROWS} * 1.5em + var(--space-4))` }}
      />
      <Button
        type="submit"
        aria-label="Send message"
        loading={busy}
        disabled={!canSend}
        iconLeft={<Icons.SendIcon />}
        className={styles.send}
      />
    </form>
  );
}
