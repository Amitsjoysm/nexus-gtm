import { useEffect, useRef } from "react";
import { Button, EmptyState, Icons } from "@/components/ui";
import type { ChatMessage } from "@/lib/types";
import styles from "./ChatThread.module.css";

export interface ChatThreadProps {
  messages: ChatMessage[];
  /** A suggestion chip was clicked; its text is passed up to prefill/send. */
  onChip?: (text: string) => void;
  /** The "Open run console" action on a handoff card. */
  onOpenRun?: (runId: string) => void;
}

/** Pull a typed `string[]` out of an untyped `data` bag. */
function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim() !== "");
}

/**
 * Scrollable conversation transcript. Renders each message by `kind` and keeps the latest
 * turn in view as new messages arrive (instantly under reduced-motion, smoothly otherwise).
 */
export function ChatThread({ messages, onChip, onOpenRun }: ChatThreadProps) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = endRef.current;
    if (!node) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className={styles.thread}>
        <EmptyState
          icon={<Icons.SparklesIcon />}
          title="Describe who you're looking for"
          description="Tell the orchestrator about the companies or people you want to find, and it will research, score, and shortlist them."
        />
      </div>
    );
  }

  return (
    <div className={styles.thread}>
      <ul className={styles.list}>
        {messages.map((message) => (
          <li key={message.id}>
            <MessageRow message={message} onChip={onChip} onOpenRun={onOpenRun} />
          </li>
        ))}
      </ul>
      <div ref={endRef} aria-hidden="true" />
    </div>
  );
}

function MessageRow({
  message,
  onChip,
  onOpenRun,
}: {
  message: ChatMessage;
  onChip?: (text: string) => void;
  onOpenRun?: (runId: string) => void;
}) {
  if (message.kind === "user") {
    return (
      <div className={styles.row} data-role="user">
        <div className={styles.bubble} data-role="user">
          {message.content}
        </div>
      </div>
    );
  }

  if (message.kind === "run_launched") {
    return <HandoffCard message={message} onOpenRun={onOpenRun} />;
  }

  const suggestions =
    message.kind === "clarifying_question" ? stringList(message.data.suggestions) : [];

  return (
    <div className={styles.row} data-role="assistant">
      <div className={styles.bubble} data-role="assistant">
        {message.content}
      </div>
      {suggestions.length > 0 && (
        <div className={styles.chips} role="group" aria-label="Suggested replies">
          {suggestions.map((text) => (
            <button
              key={text}
              type="button"
              className={styles.chip}
              onClick={() => onChip?.(text)}
              aria-label={`Use suggestion: ${text}`}
            >
              {text}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function HandoffCard({
  message,
  onOpenRun,
}: {
  message: ChatMessage;
  onOpenRun?: (runId: string) => void;
}) {
  const runId = message.data.run_id != null ? String(message.data.run_id) : null;
  const goal = typeof message.data.goal === "string" ? message.data.goal : null;

  return (
    <div className={styles.row} data-role="assistant">
      <div className={styles.handoff}>
        <span className={styles.handoffIcon} aria-hidden="true">
          <Icons.BoltIcon />
        </span>
        <div className={styles.handoffBody}>
          <h3 className={styles.handoffTitle}>Discovery run started</h3>
          {goal && <p className={styles.handoffGoal}>{goal}</p>}
          {message.content && !goal && <p className={styles.handoffGoal}>{message.content}</p>}
          {runId && onOpenRun && (
            <Button
              size="sm"
              iconRight={<Icons.ChevronRightIcon />}
              onClick={() => onOpenRun(runId)}
            >
              Open run console
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
