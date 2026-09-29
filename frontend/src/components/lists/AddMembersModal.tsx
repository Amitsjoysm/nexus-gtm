import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  EmptyState,
  ErrorState,
  Icons,
  Input,
  Modal,
  Skeleton,
  useToast,
} from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useSelection } from "@/hooks/useSelection";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { Account, ProspectList, WorkspaceContact } from "@/lib/types";
import styles from "./AddMembersModal.module.css";

/** How many matches the picker shows. Search narrows; nobody ticks through hundreds here. */
const SHOWN = 50;

interface Row {
  id: string;
  title: string;
  sub: string;
}

function useDebounced<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return settled;
}

/**
 * Find companies or people in this workspace and put them on the list. The search runs on the
 * server, so it reaches every account and contact, not only the newest page of them.
 */
export function AddMembersModal({
  open,
  onClose,
  list,
  onAdded,
}: {
  open: boolean;
  onClose: () => void;
  list: ProspectList;
  onAdded: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const people = list.kind === "contact";
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim());
  const [busy, setBusy] = useState(false);

  const found = useApi<Row[]>(
    async (signal) => {
      if (!open) return [];
      if (people) {
        const rows: WorkspaceContact[] = await api.listWorkspaceContacts(q || undefined, signal);
        return rows.slice(0, SHOWN).map((c) => ({
          id: c.id,
          title: c.full_name,
          sub: [c.title, c.account_name].filter(Boolean).join(" · "),
        }));
      }
      const rows: Account[] = await api.listAccounts(signal, q || undefined);
      return rows.slice(0, SHOWN).map((a) => ({
        id: a.id,
        title: a.name,
        sub: [a.domain, a.industry].filter(Boolean).join(" · "),
      }));
    },
    [open, q, people],
  );

  const rows = useMemo(() => found.data ?? [], [found.data]);
  const visibleIds = useMemo(() => rows.map((r) => r.id), [rows]);
  const selection = useSelection(visibleIds);

  useEffect(() => {
    if (!open) {
      setSearch("");
      selection.clear();
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  async function add() {
    if (selection.ids.length === 0) return;
    setBusy(true);
    try {
      const res = await api.addListMembers(
        list.id,
        people ? { contactIds: selection.ids } : { accountIds: selection.ids },
      );
      toast.success(
        `Added ${formatNumber(res.added)} to “${list.name}”`,
        res.already > 0 ? `${formatNumber(res.already)} were already on it.` : undefined,
      );
      onAdded();
      onClose();
    } catch (err) {
      toast.error("Couldn't add them", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const n = selection.ids.length;
  const noun = people ? (n === 1 ? "person" : "people") : n === 1 ? "company" : "companies";

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="md"
      title={people ? "Add people" : "Add companies"}
      description={`To “${list.name}”.`}
      footer={
        <>
          <span className={styles.picked} aria-live="polite">
            {n === 0 ? "Nobody ticked" : `${formatNumber(n)} ${noun} ticked`}
          </span>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={add} loading={busy} disabled={n === 0} iconLeft={<Icons.PlusIcon />}>
            Add {n > 0 ? formatNumber(n) : ""} to list
          </Button>
        </>
      }
    >
      <div className={styles.body}>
        <Input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={people ? "Search name, title, email or company" : "Search name or domain"}
          iconLeft={<Icons.SearchIcon />}
          aria-label={people ? "Search contacts" : "Search accounts"}
          autoFocus
        />

        {found.error && !found.data ? (
          <ErrorState title="Couldn't search" message={found.error.detail} onRetry={found.refetch} />
        ) : found.loading && !found.data ? (
          <Skeleton width="100%" height={220} />
        ) : rows.length === 0 ? (
          <EmptyState
            compact
            icon={<Icons.SearchIcon />}
            title="No matches"
            description={
              q
                ? `Nothing in this workspace matches “${q}”.`
                : people
                  ? "This workspace has no contacts yet."
                  : "This workspace has no accounts yet."
            }
          />
        ) : (
          <>
            <div className={styles.headRow}>
              <Checkbox
                label="Tick everyone shown"
                checked={selection.allVisible}
                indeterminate={selection.someVisible}
                onChange={selection.toggleVisible}
              />
              <span className={styles.headLabel}>
                {rows.length === SHOWN
                  ? `First ${SHOWN} matches. Search to narrow.`
                  : `${formatNumber(rows.length)} ${rows.length === 1 ? "match" : "matches"}`}
              </span>
            </div>
            <ul className={styles.results} aria-label="Matches">
              {rows.map((r) => (
                <li key={r.id}>
                  <label className={styles.row}>
                    <Checkbox
                      label={`Tick ${r.title}`}
                      checked={selection.selected.has(r.id)}
                      onChange={() => selection.toggle(r.id)}
                    />
                    <span className={styles.text}>
                      <span className={styles.title}>{r.title}</span>
                      {r.sub && <span className={styles.sub}>{r.sub}</span>}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </Modal>
  );
}
