import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Skeleton,
  Textarea,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DncBadge } from "@/components/engagement/DncBadge";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { DoNotContactEntry } from "@/lib/types";
import styles from "./DoNotContactPage.module.css";

/**
 * The workspace's do-not-contact list (spec §9, D7, D11).
 *
 * Every member sees it and can add an address, because an SDR about to email someone needs to know.
 * Lifting a block is for managers and needs a note, kept with the record. An unsubscribe is
 * permanent: it shows no Lift action at all rather than one that fails.
 */
export function DoNotContactPage() {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const canLift = session?.role === "manager" || session?.role === "admin"
    || session?.role === "owner";
  const [query, setQuery] = useState("");
  const [includeLifted, setIncludeLifted] = useState(false);
  const [address, setAddress] = useState("");
  const [adding, setAdding] = useState(false);
  const [lifting, setLifting] = useState<DoNotContactEntry | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const blocks = useApi<DoNotContactEntry[]>(
    (signal) => api.listDoNotContact({ q: query, include_lifted: includeLifted }, signal),
    [query, includeLifted],
  );

  async function add(event: FormEvent) {
    event.preventDefault();
    setAdding(true);
    try {
      await api.addDoNotContact(address.trim());
      toast.success("Blocked", `${address.trim()} will not be emailed by any campaign.`);
      setAddress("");
      blocks.refetch();
    } catch (err) {
      toast.error("Couldn't block that address", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setAdding(false);
    }
  }

  async function lift() {
    if (!lifting) return;
    setBusy(true);
    try {
      await api.liftDoNotContact(lifting.id, note.trim());
      toast.success("Block lifted", `${lifting.email} can be contacted again.`);
      setLifting(null);
      setNote("");
      blocks.refetch();
    } catch (err) {
      toast.error("Couldn't lift the block", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<DoNotContactEntry>[] = [
    { key: "email", header: "Address", sortValue: (b) => b.email,
      render: (b) => <span className={styles.mono}>{b.email}</span> },
    { key: "reason", header: "Reason", sortValue: (b) => b.reason,
      render: (b) => <DncBadge reason={b.reason} /> },
    { key: "created_at", header: "Since", hideOnMobile: true, sortValue: (b) => b.created_at,
      render: (b) => new Date(b.created_at).toLocaleDateString() },
    { key: "status", header: "Status", hideOnMobile: true, sortValue: (b) => b.lifted_at ?? "",
      render: (b) => b.lifted_at
        ? <Badge tone="neutral" title={b.lift_note}>Lifted {new Date(b.lifted_at).toLocaleDateString()}</Badge>
        : <Badge tone="danger" dot>Active</Badge> },
    { key: "actions", header: "", render: (b) => canLift && b.liftable ? (
        <Button size="sm" variant="ghost" onClick={() => setLifting(b)}>Lift</Button>
      ) : null },
  ];

  return (
    <div>
      <PageHeader
        eyebrow={<Link to="/mailboxes" className={styles.back}><Icons.ChevronLeftIcon /> My mailboxes</Link>}
        title="Do not contact"
        description="Addresses no campaign will email. Unsubscribes are permanent; a clear no, a bounce or a manual block can be lifted by a manager with a note."
      />

      <Card padding="lg" className={styles.tools}>
        <form className={styles.addForm} onSubmit={add}>
          <Field label="Block an address">
            <Input type="email" value={address} onChange={(e) => setAddress(e.target.value)}
              placeholder="name@company.com" required />
          </Field>
          <Button type="submit" loading={adding} disabled={!address.trim()}>Block</Button>
        </form>
        <div className={styles.filters}>
          <Field label="Search" hideLabel>
            <Input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Search addresses…" />
          </Field>
          <label className={styles.toggle}>
            <input type="checkbox" checked={includeLifted}
              onChange={(e) => setIncludeLifted(e.target.checked)} />
            Show lifted blocks
          </label>
        </div>
      </Card>

      <DataState
        state={blocks}
        errorTitle="Couldn't load the do-not-contact list"
        skeleton={<Skeleton width="100%" height={240} />}
        isEmpty={(rows) => rows.length === 0}
        empty={<EmptyState icon={<Icons.ShieldCheckIcon />} title="Nobody is blocked"
          description="Unsubscribes, clear no replies and bounces appear here automatically." />}
      >
        {(rows) => <DataTable columns={columns} rows={rows} getRowKey={(b) => b.id} caption="Do-not-contact list" />}
      </DataState>

      <Modal
        open={lifting !== null}
        onClose={() => setLifting(null)}
        title={`Lift the block on ${lifting?.email ?? ""}?`}
        description="Campaigns may email this address again. Your note is kept with the record."
        footer={
          <>
            <Button variant="ghost" onClick={() => setLifting(null)}>Cancel</Button>
            <Button variant="danger" onClick={lift} loading={busy} disabled={note.trim().length < 5}>
              Lift block
            </Button>
          </>
        }
      >
        <Field label="Why" hint="For example: they asked to hear from us again on a call.">
          <Textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Modal>
    </div>
  );
}

export default DoNotContactPage;
