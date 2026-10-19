import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button, ErrorState, Modal, SkeletonText, SwitchRow, WorkingIndicator, useToast,
} from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { useApi } from "@/hooks/useApi";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type {
  DeliverAction, ProspectDeliverResult, ProspectQuote, ProspectSelection, QuoteAction,
} from "@/lib/prospectorTypes";
import { ListPicker, listChoiceReady } from "./ListPicker";
import type { ListChoice } from "./ListPicker";
import styles from "./Prospector.module.css";

export interface DeliveryRequest {
  action: QuoteAction;
  selection: ProspectSelection;
  /** Rows chosen, or the select-all limit. */
  count: number;
}

const FIELD_LINES: [string, string][] = [
  ["prospector.email", "Emails"],
  ["prospector.phone", "Direct phones"],
  ["prospector.company", "Companies"],
];

function requestId(): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
  return `px-${random}`.slice(0, 64);
}

function titleFor(req: DeliveryRequest): string {
  const people = req.selection.view === "contacts";
  const n = req.selection.all_matching ? `up to ${formatNumber(req.count)}`
    : formatNumber(req.count);
  const noun = people ? (req.count === 1 ? "contact" : "contacts")
    : (req.count === 1 ? "company" : "companies");
  switch (req.action) {
    case "reveal": return people ? "Reveal this contact" : "Reveal this company";
    case "list": return `Add ${n} ${noun} to a list`;
    case "company_list": return "Add their companies to an account list";
    case "export": return `Export ${n} ${noun} to CSV`;
    default: return `Save ${n} ${noun}`;
  }
}

const DONE_TITLE: Record<DeliverAction, string> = {
  save: "Saved to your workspace",
  reveal: "Revealed and saved",
  list: "Added to the list",
  company_list: "Companies added to the list",
};

function doneMessage(action: QuoteAction, r: ProspectDeliverResult): string {
  const parts = [];
  if (r.created) parts.push(`${formatNumber(r.created)} added`);
  if (r.reused) parts.push(`${formatNumber(r.reused)} already there`);
  if (action === "list" || action === "company_list") parts.push(`${formatNumber(r.listed)} on the list`);
  if (r.unavailable) parts.push(`${formatNumber(r.unavailable)} no longer available`);
  return parts.join(", ") + ".";
}

/**
 * The confirm step before anything is spent: what the selection costs field by field at today's
 * rate cards, what is free because it was already paid for or already held, the balance, and (for
 * people) whether to buy direct phones. Confirming delivers or exports under one request id, so a
 * double click or a retry never pays twice.
 */
export function ConfirmDeliveryDialog({ request, onClose, onDone }: {
  request: DeliveryRequest | null;
  onClose: () => void;
  onDone: (result: ProspectDeliverResult | null) => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [includePhones, setIncludePhones] = useState(false);
  const [busy, setBusy] = useState(false);
  const [choice, setChoice] = useState<ListChoice>({ mode: "existing", listId: "", name: "" });
  const id = useMemo(() => (request ? requestId() : ""), [request]);
  const open = request !== null;
  const people = request?.selection.view === "contacts";
  const phonesOffered = people && request?.action !== "company_list";
  const listKind = request?.action === "list" && people ? "contact" : "account";
  const needsList = request?.action === "list" || request?.action === "company_list";

  useEffect(() => {
    if (open) {
      setIncludePhones(false);
      setChoice({ mode: "existing", listId: "", name: "" });
    }
  }, [open, request]);

  const quote = useApi<ProspectQuote | null>(
    (signal) => request
      ? api.prospectorQuote({ selection: request.selection, action: request.action,
        include_phones: includePhones }, signal)
      : Promise.resolve(null),
    [request, includePhones],
  );
  const q = quote.data;
  const ready = !!q && q.enough && (!needsList || listChoiceReady(choice));

  async function confirm() {
    if (!request || !ready || busy) return;
    setBusy(true);
    try {
      let listId: string | null = null;
      if (needsList) {
        listId = choice.mode === "existing" ? choice.listId
          : (await api.createList(choice.name.trim(), listKind)).id;
      }
      if (request.action === "export") {
        const out = await api.prospectorExport({ selection: request.selection,
          include_phones: includePhones, request_id: id });
        toast.toast({ tone: "success", title: "Export ready",
          description: `${formatNumber(out.rows ?? request.count)} rows downloaded.` });
        onDone(null);
      } else {
        const result = await api.prospectorDeliver({
          selection: request.selection, action: request.action as DeliverAction,
          include_phones: includePhones, list_id: listId, request_id: id,
        });
        toast.toast({
          tone: "success", title: DONE_TITLE[request.action as DeliverAction],
          description: doneMessage(request.action, result),
          action: listId ? { label: "Open list", onClick: () => navigate(`/lists/${listId}`) }
            : undefined,
        });
        onDone(result);
      }
      onClose();
    } catch (err) {
      toast.error("That did not go through",
        err instanceof ApiError ? err.detail : "Please try again. Nothing was charged.");
    } finally {
      setBusy(false);
    }
  }

  const total = q?.total_credits ?? 0;
  const primary = total > 0 ? `Spend ${formatNumber(total)} credit${total === 1 ? "" : "s"}`
    : request?.action === "export" ? "Export" : "Confirm";

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onClose}
      size="md"
      title={request ? titleFor(request) : ""}
      description="Charged once per email, phone or company. Anything already paid for or already in your workspace is free."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button onClick={confirm} loading={busy} disabled={!ready}>{primary}</Button>
        </>
      }
    >
      {busy ? (
        <WorkingIndicator
          label={request?.action === "export" ? "Preparing the file" : "Saving to your workspace"}
          hint="Large selections read up to two pages of 500 from the data provider."
          slowAfter={20}
          slowHint="Still working: the data provider is answering slowly."
        />
      ) : quote.loading && !q ? (
        <SkeletonText lines={4} />
      ) : quote.error ? (
        <ErrorState compact title="Couldn't price this selection" message={quote.error.detail}
          onRetry={quote.refetch} />
      ) : q ? (
        <div className={styles.quote}>
          <dl className={styles.lines}>
            {FIELD_LINES.filter(([cap]) => q.charges[cap]).map(([cap, label]) => (
              <div key={cap} className={styles.line}>
                <dt>{label}</dt>
                <dd>{formatNumber(q.charges[cap])} × {formatNumber(q.credits[cap] / q.charges[cap])}
                  {" "}= <strong>{formatNumber(q.credits[cap])}</strong></dd>
              </div>
            ))}
            {q.already_paid > 0 && (
              <div className={styles.line}><dt>Already paid for</dt><dd>{formatNumber(q.already_paid)} free</dd></div>
            )}
            {q.held > 0 && (
              <div className={styles.line}><dt>Already in your workspace</dt><dd>{formatNumber(q.held)} free</dd></div>
            )}
            {q.unavailable > 0 && (
              <div className={styles.line}><dt>No longer available</dt><dd>{formatNumber(q.unavailable)} skipped</dd></div>
            )}
            <div className={styles.totalLine}>
              <dt>Total</dt>
              <dd>{formatNumber(total)} credit{total === 1 ? "" : "s"}</dd>
            </div>
            {q.balance !== null && (
              <div className={styles.line}><dt>Your balance</dt><dd>{formatNumber(q.balance)} credits</dd></div>
            )}
          </dl>
          {!q.enough && (
            <p className={styles.warn} role="alert">
              Not enough credits: this needs {formatNumber(total)} and the balance is{" "}
              {formatNumber(q.balance ?? 0)}. Choose fewer rows, or add credits in Billing.
            </p>
          )}
          {phonesOffered && (
            <SwitchRow
              label="Include direct phones"
              hint="1 credit per number. Off: phone numbers are not bought or saved."
              checked={includePhones}
              onChange={setIncludePhones}
            />
          )}
          {people && request?.action !== "company_list" && request?.action !== "export" && (
            <p className={styles.secondary}>
              Emails are saved as delivered and held out of campaigns until you verify them.
            </p>
          )}
          {needsList && <ListPicker kind={listKind} value={choice} onChange={setChoice} />}
        </div>
      ) : null}
    </Modal>
  );
}
