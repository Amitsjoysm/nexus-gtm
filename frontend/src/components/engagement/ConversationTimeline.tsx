import type { ReactNode } from "react";
import { Badge } from "@/components/ui";
import type { ReplyCategory } from "@/lib/types";
import { CATEGORY, when } from "./labels";
import styles from "./ConversationTimeline.module.css";

/**
 * A conversation, oldest first: what the SDR sent and what came back.
 *
 * One component for two readers. The reply desk passes whole bodies for the thread it is answering;
 * the contact and account pages pass previews across every campaign. Direction is carried by
 * alignment, a label and the surface, never by colour alone.
 */

export interface TimelineMessage {
  id: string;
  direction: "out" | "in";
  subject: string;
  body: string;
  at: string | null;
  /** Who, when the timeline spans several people (an account). */
  who?: string;
  /** Where it came from: a campaign name, or "One-off email". */
  context?: string;
  category?: ReplyCategory | null;
}

export interface ConversationTimelineProps {
  messages: TimelineMessage[];
  /** Shown when there is nothing yet. */
  empty?: ReactNode;
  /** Clamp long bodies to a few lines (previews) instead of showing them whole. */
  clamp?: boolean;
}

export function ConversationTimeline({ messages, empty, clamp = false }: ConversationTimelineProps) {
  if (messages.length === 0) {
    return <p className={styles.empty}>{empty ?? "Nothing has been sent or received yet."}</p>;
  }
  return (
    <ol className={styles.timeline}>
      {messages.map((m) => {
        const outbound = m.direction === "out";
        const category = m.category ? CATEGORY[m.category] : null;
        return (
          <li key={m.id} className={outbound ? styles.out : styles.in}>
            <article className={styles.message} aria-label={outbound ? "Sent" : "Received"}>
              <header className={styles.meta}>
                <span className={styles.direction}>{outbound ? "You sent" : m.who ? `${m.who} replied` : "They replied"}</span>
                <time dateTime={m.at ?? undefined} className={styles.time}>{when(m.at)}</time>
                {category && <Badge tone={category.tone}>{category.label}</Badge>}
              </header>
              {m.subject && <p className={styles.subject}>{m.subject}</p>}
              <p className={clamp ? `${styles.body} ${styles.clamped}` : styles.body}>{m.body}</p>
              {m.context && <p className={styles.context}>{m.context}</p>}
            </article>
          </li>
        );
      })}
    </ol>
  );
}
