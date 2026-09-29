import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Field, Input, Modal, Select, Skeleton, useToast } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { ListKind, ProspectList } from "@/lib/types";
import styles from "./AddToListModal.module.css";

export interface AddToListModalProps {
  open: boolean;
  onClose: () => void;
  kind: ListKind;
  /** Companies for an account list. */
  accountIds?: string[];
  /** People for a contact list. */
  contactIds?: string[];
  onDone?: (listId: string) => void;
}

function noun(kind: ListKind, n: number) {
  if (kind === "account") return n === 1 ? "company" : "companies";
  return n === 1 ? "person" : "people";
}

/**
 * Put a selection on a list: one you can already change, or a new one named here. Offered from
 * Accounts, Contacts and discovery results, so the same choice reads the same everywhere.
 *
 * Only lists of the matching kind are offered. A company cannot go on a contact list, and the
 * server refuses it, so offering the choice would only produce an error.
 */
export function AddToListModal({
  open,
  onClose,
  kind,
  accountIds = [],
  contactIds = [],
  onDone,
}: AddToListModalProps) {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const lists = useApi<ProspectList[]>(
    (signal) => (open ? api.listSavedLists(signal, kind) : Promise.resolve([])),
    [open, kind],
  );
  const mine = useMemo(() => (lists.data ?? []).filter((l) => l.can_edit), [lists.data]);
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [listId, setListId] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const count = kind === "account" ? accountIds.length : contactIds.length;

  // Start on "existing" when there is something to add to, otherwise go straight to naming one.
  useEffect(() => {
    if (!open || lists.loading) return;
    setMode(mine.length > 0 ? "existing" : "new");
    setListId((current) => current || mine[0]?.id || "");
  }, [open, lists.loading, mine]);

  useEffect(() => {
    if (!open) {
      setName("");
      setListId("");
    }
  }, [open]);

  const ids = kind === "account" ? { accountIds } : { contactIds };
  const ready = mode === "existing" ? !!listId : name.trim() !== "";

  async function submit() {
    if (!ready || busy) return;
    setBusy(true);
    try {
      let target: { id: string; name: string };
      let added: number;
      let already = 0;
      if (mode === "existing") {
        const res = await api.addListMembers(listId, ids);
        target = { id: listId, name: mine.find((l) => l.id === listId)?.name ?? "the list" };
        added = res.added;
        already = res.already;
      } else {
        const res = await api.createList(name.trim(), kind, ids);
        target = { id: res.id, name: res.name };
        added = res.added ?? res.members;
      }
      toast.toast({
        tone: "success",
        title: `Added to “${target.name}”`,
        description:
          `${formatNumber(added)} ${noun(kind, added)} added` +
          (already > 0 ? `, ${formatNumber(already)} already on it.` : "."),
        action: { label: "Open list", onClick: () => navigate(`/lists/${target.id}`) },
      });
      onDone?.(target.id);
      onClose();
    } catch (err) {
      toast.error(
        "Couldn't add to the list",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  const kindLabel = kind === "account" ? "account list" : "contact list";

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="sm"
      title={`Add ${formatNumber(count)} ${noun(kind, count)} to a list`}
      description={`Goes on an ${kindLabel}, which you can open from Lists and start a campaign from.`}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy} disabled={!ready}>
            {mode === "new" ? "Create list and add" : "Add to list"}
          </Button>
        </>
      }
    >
      {lists.loading && !lists.data ? (
        <Skeleton width="100%" height={88} />
      ) : (
        <form
          className={styles.form}
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <fieldset className={styles.choices}>
            <legend className={styles.legend}>Where to</legend>
            <label className={styles.choice}>
              <input
                type="radio"
                name="add-to-list-mode"
                checked={mode === "existing"}
                disabled={mine.length === 0}
                onChange={() => setMode("existing")}
              />
              <span>
                An existing list
                {mine.length === 0 && (
                  <span className={styles.aside}> (you have no {kindLabel}s yet)</span>
                )}
              </span>
            </label>
            <label className={styles.choice}>
              <input
                type="radio"
                name="add-to-list-mode"
                checked={mode === "new"}
                onChange={() => setMode("new")}
              />
              <span>A new list</span>
            </label>
          </fieldset>

          {mode === "existing" ? (
            <Field label="List">
              <Select
                value={listId}
                onChange={(e) => setListId(e.target.value)}
                options={mine.map((l) => ({
                  value: l.id,
                  label: `${l.name} (${formatNumber(l.members)})`,
                }))}
              />
            </Field>
          ) : (
            <Field label="List name" required>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={200}
                placeholder={kind === "account" ? "Tier 1 fintech" : "Heads of RevOps"}
                autoFocus
              />
            </Field>
          )}
        </form>
      )}
    </Modal>
  );
}
