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
  /** Contacts view: which paid fields the dialog opens with (email on, phones off by default). */
  fields?: { emails: boolean; phones: boolean };
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
  const phonesOnly = people && req.fields?.phones && !req.fields.emails;
  switch (req.action) {
    case "reveal":
      if (!people) return "Reveal this company";
      return phonesOnly ? "Reveal this direct phone"
        : req.fields?.emails && !req.fields.phones ? "Reveal this email" : "Reveal this contact";
    case "list": return `Add ${n} ${noun} to a list`;
    case "company_list": return "Add their companies to an account list";
    case "export": return `Export ${n} ${noun} to CSV`;
    default: return phonesOnly ? `Reveal direct phones for ${n} ${noun}` : `Save ${n} ${noun}`;
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
  if (r.filled) parts.push(`${formatNumber(r.filled)} updated with what was missing`);
  if (r.reused - r.filled > 0) parts.push(`${formatNumber(r.reused - r.filled)} already there`);
  if (action === "list" || action === "company_list") parts.push(`${formatNumber(r.listed)} on the list`);
  if (r.unavailable) parts.push(`${formatNumber(r.unavailable)} no longer available`);
  return parts.length ? parts.join(", ") + "." : "Nothing new to add.";
}

/** "6 of 25 selected have a direct phone": the honest size of a field choice before it is bought.
 *  `noun` is bare ("email", "direct phone"); the article is added here. */
function availability(have: number, of: number, noun: string): string {
  const one = /^[aeiou]/i.test(noun) ? `an ${noun}` : `a ${noun}`;
  if (of === 1) return have ? `This contact has ${one}.` : `This contact has no ${noun}.`;
  return `${formatNumber(have)} of ${formatNumber(of)} selected have ${one}.`;
}

function priceText(q: ProspectQuote, cap: string, noun: string): string {
  const n = q.charges[cap] ?? 0;
  if (!n) return "Nothing to pay: already yours or already paid for.";
  const many = `${formatNumber(n)} new ${noun}${n === 1 ? "" : "s"}`;
  if (!q.uses_credits) return `${many}, included in your plan.`;
  const total = q.credits[cap] ?? 0;
  return `${many}, ${formatNumber(total)} credit${total === 1 ? "" : "s"}.`;
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
  const [includeEmails, setIncludeEmails] = useState(true);
  const [includePhones, setIncludePhones] = useState(false);
  const [busy, setBusy] = useState(false);
  const [choice, setChoice] = useState<ListChoice>({ mode: "existing", listId: "", name: "" });
  const id = useMemo(() => (request ? requestId() : ""), [request]);
  const open = request !== null;
  const people = request?.selection.view === "contacts";
  // Email and phone are chosen separately for people (2026-11-18), on every paid action.
  const fieldsOffered = people && request?.action !== "company_list";
  const listKind = request?.action === "list" && people ? "contact" : "account";
  const needsList = request?.action === "list" || request?.action === "company_list";

  useEffect(() => {
    if (open) {
      setIncludeEmails(request?.fields?.emails ?? true);
      setIncludePhones(request?.fields?.phones ?? false);
      setChoice({ mode: "existing", listId: "", name: "" });
    }
  }, [open, request]);

  const quote = useApi<ProspectQuote | null>(
    (signal) => request
      ? api.prospectorQuote({ selection: request.selection, action: request.action,
        include_emails: fieldsOffered ? includeEmails : true,
        include_phones: fieldsOffered ? includePhones : false }, signal)
      : Promise.resolve(null),
    [request, includeEmails, includePhones],
  );
  const q = quote.data;
  // Every selected row has gone from the provider's page (its results reordered since the search).
  const vanished = !!q && q.records === 0 && q.unavailable > 0;
  // A reveal with nothing chosen would buy nothing and save a contact the rep did not ask for.
  const nothingChosen = request?.action === "reveal" && fieldsOffered
    && !includeEmails && !includePhones;
  const ready = !!q && q.enough && !vanished && !nothingChosen
    && (!needsList || listChoiceReady(choice));

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
          include_emails: fieldsOffered ? includeEmails : true,
          include_phones: fieldsOffered ? includePhones : false, request_id: id });
        toast.toast({ tone: "success", title: "Export ready",
          description: `${formatNumber(out.rows ?? request.count)} rows downloaded.` });
        onDone(null);
      } else {
        const result = await api.prospectorDeliver({
          selection: request.selection, action: request.action as DeliverAction,
          include_emails: fieldsOffered ? includeEmails : true,
          include_phones: fieldsOffered ? includePhones : false, list_id: listId,
          request_id: id,
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
  const spends = total > 0 && q?.uses_credits !== false;
  const primary = spends ? `Spend ${formatNumber(total)} credit${total === 1 ? "" : "s"}`
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
                {q.uses_credits ? (
                  <dd>{formatNumber(q.charges[cap])} × {formatNumber(q.credits[cap] / q.charges[cap])}
                    {" "}= <strong>{formatNumber(q.credits[cap])}</strong></dd>
                ) : (
                  <dd>{formatNumber(q.charges[cap])}</dd>
                )}
              </div>
            ))}
            {q.already_paid > 0 && (
              <div className={styles.line}><dt>Already paid for</dt><dd>{formatNumber(q.already_paid)} free</dd></div>
            )}
            {q.held > 0 && (
              <div className={styles.line}>
                <dt>Already in your workspace</dt>
                <dd>{q.filled ? `${formatNumber(q.filled)} of ${formatNumber(q.held)} get what they were missing`
                  : `${formatNumber(q.held)} free`}</dd>
              </div>
            )}
            {q.unavailable > 0 && (
              <div className={styles.line}><dt>No longer available</dt><dd>{formatNumber(q.unavailable)} skipped</dd></div>
            )}
            <div className={styles.totalLine}>
              <dt>Total</dt>
              {/* A plan that takes no credits (unlimited or internal) is told so, rather than
                  shown a price beside a balance of 0 it never draws on. */}
              <dd>{q.uses_credits ? `${formatNumber(total)} credit${total === 1 ? "" : "s"}`
                : "Included in your plan"}</dd>
            </div>
            {q.uses_credits && q.balance !== null && (
              <div className={styles.line}><dt>Your balance</dt><dd>{formatNumber(q.balance)} credits</dd></div>
            )}
          </dl>
          {vanished ? (
            <p className={styles.warn} role="alert">
              These rows are no longer on the data provider's results page: its results changed
              since you searched. Run the search again and select them from the new page. Nothing
              was charged.
            </p>
          ) : !q.enough && (
            <p className={styles.warn} role="alert">
              {q.reason ?? "This cannot be delivered right now."}
            </p>
          )}
          {fieldsOffered && !vanished && (
            <div className={styles.fieldChoices}>
              <SwitchRow
                label="Emails"
                hint={`${availability(q.with_email, q.records, "email")} ${includeEmails ? priceText(q, "prospector.email", "email") : "Off: emails are not bought or saved."}`}
                checked={includeEmails}
                onChange={setIncludeEmails}
              />
              <SwitchRow
                label="Direct phones"
                hint={`${availability(q.with_phone, q.records, "direct phone")} ${includePhones ? priceText(q, "prospector.phone", "phone") : "Off: phones are not bought or saved."}`}
                checked={includePhones}
                onChange={setIncludePhones}
              />
            </div>
          )}
          {nothingChosen && (
            <p className={styles.secondary}>Choose the email, the direct phone, or both.</p>
          )}
          {people && includeEmails && request?.action !== "company_list"
            && request?.action !== "export" && (
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
