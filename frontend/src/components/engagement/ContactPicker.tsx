import { useEffect, useMemo, useState } from "react";
import {
  Badge, Button, DataTable, EmptyState, ErrorState, Field, Icons, Input, Select,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { EngagementCandidate, ProspectList } from "@/lib/types";
import { InsightBadge, useContactInsights } from "./InsightBadge";
import styles from "./ContactPicker.module.css";

/**
 * Choose who a campaign emails (spec §9, step 1): from a saved list, by title and seniority, or by
 * search. A saved list holds accounts, so the server expands it to the people there; this screen
 * only chooses among them.
 *
 * People on the do-not-contact list are shown and cannot be ticked, so an SDR who expected to see
 * someone learns why they are missing rather than wondering.
 */

const SENIORITY = [
  { value: "", label: "Any seniority" },
  { value: "c_level", label: "C-level" },
  { value: "vp", label: "VP" },
  { value: "director", label: "Director" },
  { value: "manager", label: "Manager" },
  { value: "ic", label: "Individual contributor" },
];

function useDebounced<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return settled;
}

export interface ContactPickerProps {
  /** Contacts already in the campaign: shown as added, not offered again. */
  enrolledIds: ReadonlySet<string>;
  onAdd: (contactIds: string[]) => Promise<void>;
}

export function ContactPicker({ enrolledIds, onAdd }: ContactPickerProps) {
  const api = useApiClient();
  const [listId, setListId] = useState("");
  const [q, setQ] = useState("");
  const [title, setTitle] = useState("");
  const [seniority, setSeniority] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  // Debounce a STRING: an object literal is a new value every render, so the timer would re-arm
  // forever and refetch every 300ms. Equal strings let React skip the update.
  const settled = useDebounced(JSON.stringify({ listId, q, title, seniority }));
  const query = useMemo(
    () => JSON.parse(settled) as { listId: string; q: string; title: string; seniority: string },
    [settled],
  );

  const lists = useApi<ProspectList[]>((s) => api.listSavedLists(s), []);
  const found = useApi<EngagementCandidate[]>(
    (s) => api.engagementCandidates({
      list_id: query.listId || undefined, q: query.q || undefined,
      title: query.title || undefined, seniority: query.seniority || undefined,
    }, s),
    [query.listId, query.q, query.title, query.seniority],
  );

  const rows = found.data ?? [];
  // The likelihood badge for the first hundred shown: one request, not one per row.
  const insights = useContactInsights(rows.slice(0, 100).map((r) => r.contact_id));
  const selectable = useMemo(
    () => rows.filter((r) => !r.blocked && !enrolledIds.has(r.contact_id)),
    [rows, enrolledIds],
  );
  const allPicked = selectable.length > 0 && selectable.every((r) => picked.has(r.contact_id));

  function toggle(id: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function toggleAll() {
    setPicked((prev) => {
      const next = new Set(prev);
      for (const r of selectable) {
        if (allPicked) next.delete(r.contact_id);
        else next.add(r.contact_id);
      }
      return next;
    });
  }
  async function add() {
    setAdding(true);
    try {
      await onAdd([...picked]);
      setPicked(new Set());
    } finally {
      setAdding(false);
    }
  }

  const columns: Column<EngagementCandidate>[] = [
    {
      key: "pick",
      width: "44px",
      header: (
        <input
          type="checkbox" className={styles.check} aria-label="Select everyone shown"
          checked={allPicked} onChange={toggleAll} disabled={selectable.length === 0}
        />
      ),
      render: (r) => {
        const added = enrolledIds.has(r.contact_id);
        return (
          <input
            type="checkbox" className={styles.check}
            aria-label={`Select ${r.full_name}`}
            checked={added || picked.has(r.contact_id)}
            disabled={r.blocked || added}
            onChange={() => toggle(r.contact_id)}
          />
        );
      },
    },
    {
      key: "person",
      header: "Person",
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.name}>{r.full_name}</span>
          {r.title && <span className={styles.sub}>{r.title}</span>}
          <InsightBadge insight={insights.get(r.contact_id)} compact />
        </div>
      ),
    },
    { key: "account", header: "Account", render: (r) => r.account_name, hideOnMobile: true },
    {
      key: "email",
      header: "Email",
      hideOnMobile: true,
      render: (r) => (
        <div className={styles.person}>
          <span className={styles.sub}>{r.email}</span>
          {r.blocked ? (
            <Badge tone="danger">Do not contact</Badge>
          ) : enrolledIds.has(r.contact_id) ? (
            <Badge tone="accent">Added</Badge>
          ) : r.email_status === "invalid" ? (
            <Badge tone="warning">Address invalid</Badge>
          ) : null}
        </div>
      ),
    },
  ];

  const listOptions = [
    { value: "", label: "All contacts" },
    ...(lists.data ?? []).map((l) => ({ value: l.id, label: `${l.name} (${l.accounts} accounts)` })),
  ];

  return (
    <div className={styles.picker}>
      <div className={styles.filters}>
        <Field label="From a saved list">
          <Select value={listId} onChange={(e) => setListId(e.target.value)} options={listOptions} />
        </Field>
        <Field label="Title contains" hint="Separate several with commas.">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="VP, Head of" />
        </Field>
        <Field label="Seniority">
          <Select value={seniority} onChange={(e) => setSeniority(e.target.value)} options={SENIORITY} />
        </Field>
        <Field label="Search">
          <Input
            type="search" value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="Name, email or company"
          />
        </Field>
      </div>

      {found.error ? (
        <ErrorState title="Couldn't load contacts" message={found.error.detail} onRetry={found.refetch} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          getRowKey={(r) => r.contact_id}
          loading={found.loading && !found.data}
          density="compact"
          caption="People who can be added to this campaign"
          empty={
            <EmptyState
              compact
              icon={<Icons.UsersIcon />}
              title="No one matches"
              description="Only contacts with an email address can be added. Try a wider title or another list."
            />
          }
        />
      )}

      <div className={styles.footer}>
        <span className={styles.count} aria-live="polite">
          {picked.size === 0 ? "Nobody selected" : `${picked.size} selected`}
        </span>
        <Button onClick={add} disabled={picked.size === 0} loading={adding} iconLeft={<Icons.PlusIcon />}>
          Add {picked.size || ""} to the campaign
        </Button>
      </div>
    </div>
  );
}
