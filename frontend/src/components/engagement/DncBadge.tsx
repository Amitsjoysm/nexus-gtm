import { useEffect, useState } from "react";
import { Badge } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";

const REASON_LABEL: Record<string, string> = {
  unsubscribed: "Unsubscribed",
  declined: "Said no",
  bounced: "Bounced",
  manual: "Blocked",
};

/** "Do not contact" next to an address, with why (spec §9 contact badge, D7). */
export function DncBadge({ reason }: { reason: string }) {
  return (
    <Badge tone="danger" dot title="No campaign will email this address">
      Do not contact · {REASON_LABEL[reason] ?? reason}
    </Badge>
  );
}

/**
 * `{lowercased address: reason}` for the blocked addresses among `emails`, checked in one request.
 *
 * Fails open to "nothing blocked": a badge that cannot load must not take the list down with it.
 * The server is what actually refuses to send (pre-send check 1, phase 07).
 */
export function useDoNotContact(emails: Array<string | null | undefined>): Record<string, string> {
  const api = useApiClient();
  const [blocked, setBlocked] = useState<Record<string, string>>({});
  const key = Array.from(
    new Set(emails.map((e) => (e ?? "").trim().toLowerCase()).filter(Boolean)),
  ).sort().slice(0, 500).join(",");

  useEffect(() => {
    if (!key) {
      setBlocked({});
      return;
    }
    const controller = new AbortController();
    api.checkDoNotContact(key.split(","), controller.signal)
      .then(setBlocked)
      .catch(() => {
        if (!controller.signal.aborted) setBlocked({});
      });
    return () => controller.abort();
  }, [api, key]);

  return blocked;
}
