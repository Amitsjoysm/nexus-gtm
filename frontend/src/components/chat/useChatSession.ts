import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ChatMessage, ChatSession, ChatStreamEvent } from "@/lib/types";

export interface UseChatSession {
  session: ChatSession | null;
  /** Ordered by `seq` ascending, deduped by `id`. */
  messages: ChatMessage[];
  /** Initial load only — not raised by `send` or the stream. */
  loading: boolean;
  error: ApiError | null;
  /** A `postChatMessage` is in flight. */
  sending: boolean;
  /** The SSE stream is currently connected. */
  streaming: boolean;
  send: (content: string) => Promise<void>;
  reload: () => void;
}

/** Build a `ChatMessage` from a stream event; fall back to a synthesized id. */
function eventToMessage(event: ChatStreamEvent): ChatMessage | null {
  const { data } = event;
  const id = data.id ?? `seq-${event.seq}`;
  const role = data.role === "user" ? "user" : "assistant";
  return {
    id,
    seq: event.seq,
    role,
    kind: data.kind ?? "assistant",
    content: data.content ?? "",
    data: data.data ?? {},
    created_at: new Date().toISOString(),
  };
}

/**
 * Drive one orchestrator chat session: initial fetch, optimistic send, and a live SSE
 * follow that reconciles streamed/posted messages.
 *
 * Messages are held in a `Map` keyed by `id` so optimistic, streamed, and posted copies
 * collapse to a single entry. The optimistic user message uses a `local-` id and a
 * fractional `seq` so it sorts last; it is dropped once the server echoes the real turn.
 */
export function useChatSession(sessionId: string): UseChatSession {
  const api = useApiClient();
  const [messageMap, setMessageMap] = useState<Map<string, ChatMessage>>(() => new Map());
  const [session, setSession] = useState<ChatSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [sending, setSending] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [streamEpoch, setStreamEpoch] = useState(0);

  // Highest server `seq` seen, for SSE resume and the optimistic ordering offset.
  const lastSeqRef = useRef(0);

  const upsert = useCallback((incoming: ChatMessage[], dropLocal = false) => {
    setMessageMap((prev) => {
      const next = new Map(prev);
      if (dropLocal) {
        for (const key of next.keys()) {
          if (key.startsWith("local-")) next.delete(key);
        }
      }
      for (const msg of incoming) {
        if (msg.seq > lastSeqRef.current) lastSeqRef.current = msg.seq;
        next.set(msg.id, msg);
      }
      return next;
    });
  }, []);

  // ---- initial load (re-runs on reload) ----
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    lastSeqRef.current = 0;

    api
      .getChatSession(sessionId, controller.signal)
      .then((res) => {
        if (cancelled) return;
        const seeded = new Map<string, ChatMessage>();
        for (const msg of res.messages) {
          if (msg.seq > lastSeqRef.current) lastSeqRef.current = msg.seq;
          seeded.set(msg.id, msg);
        }
        setMessageMap(seeded);
        setSession(res.session);
        setLoading(false);
        // Open (or re-open) the live follow once the snapshot has landed.
        setStreamEpoch((e) => e + 1);
      })
      .catch((err) => {
        if (cancelled || (err instanceof DOMException && err.name === "AbortError")) return;
        setError(err instanceof ApiError ? err : new ApiError(0, "Couldn't load the conversation."));
        setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [api, sessionId, epoch]);

  // ---- live SSE follow ----
  // Mirrors RunDetailPage's useRunActivity: open a stream, track the last seq, and on a
  // clean close reconnect via an epoch bump (debounced) as long as we're still mounted.
  useEffect(() => {
    if (streamEpoch === 0) return; // not yet seeded
    const controller = new AbortController();
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    setStreaming(true);

    api
      .streamChatEvents(
        sessionId,
        {
          lastEventId: lastSeqRef.current || undefined,
          onEvent: (event) => {
            const msg = eventToMessage(event);
            if (msg) upsert([msg], true);
          },
        },
        controller.signal,
      )
      .then(() => {
        if (cancelled) return;
        setStreaming(false);
        // Clean close (idle/ceiling). Reconnect after a short debounce.
        reconnectTimer = setTimeout(() => {
          if (!cancelled) setStreamEpoch((e) => e + 1);
        }, 1200);
      })
      .catch(() => {
        if (!cancelled) setStreaming(false);
      });

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      controller.abort();
    };
  }, [api, sessionId, streamEpoch, upsert]);

  const send = useCallback(
    async (content: string) => {
      setSending(true);
      const localId = `local-${Date.now()}`;
      const optimistic: ChatMessage = {
        id: localId,
        seq: lastSeqRef.current + 0.001,
        role: "user",
        kind: "user",
        content,
        data: {},
        created_at: new Date().toISOString(),
      };
      setMessageMap((prev) => {
        const next = new Map(prev);
        next.set(localId, optimistic);
        return next;
      });

      try {
        const res = await api.postChatMessage(sessionId, content);
        // The post returns only the appended turn. Upsert it and drop the optimistic copy.
        upsert(res.messages, true);
        setSession(res.session);
      } catch (err) {
        // Roll back the optimistic message and surface the failure to the caller.
        setMessageMap((prev) => {
          const next = new Map(prev);
          next.delete(localId);
          return next;
        });
        const apiErr =
          err instanceof ApiError ? err : new ApiError(0, "Couldn't send your message.");
        setError(apiErr);
        throw apiErr;
      } finally {
        setSending(false);
      }
    },
    [api, sessionId, upsert],
  );

  const reload = useCallback(() => setEpoch((e) => e + 1), []);

  const messages = useMemo(
    () => Array.from(messageMap.values()).sort((a, b) => a.seq - b.seq),
    [messageMap],
  );

  return { session, messages, loading, error, sending, streaming, send, reload };
}
