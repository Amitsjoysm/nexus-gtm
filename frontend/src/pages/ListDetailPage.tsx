import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Icons,
  Input,
  Modal,
  Skeleton,
  Spinner,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { AddMembersModal } from "@/components/lists/AddMembersModal";
import { useApi } from "@/hooks/useApi";
import { useSelection } from "@/hooks/useSelection";
import { useApiClient } from "@/app/AuthContext";
import { useCurrentUserId } from "@/app/useCurrentUserId";
import { useEngineOn } from "@/app/EngagementContext";
import { ApiError } from "@/lib/api";
import { formatNumber, timeAgo } from "@/lib/format";
import type { ListMember, ListMembersPage, ProspectList } from "@/lib/types";
import styles from "./ListDetailPage.module.css";

const PAGE_SIZE = 100;

function useDebounced<T>(value: T, ms = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return settled;
}

/**
 * One list: who is on it, adding and removing members, and starting a campaign from it.
 *
 * Membership is exactly what someone put there. Nothing re-runs a filter behind the page, so the
 * campaign started from it emails the people shown here and nobody else.
 */
export function ListDetailPage() {
  const { listId = "" } = useParams();
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const me = useCurrentUserId();
  // Unknown reads as on, as everywhere else; the campaign builder says so if it is off.
  const engineOn = useEngineOn() !== false;

  const list = useApi<ProspectList>((signal) => api.getList(listId, signal), [listId]);
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim());
  const [offset, setOffset] = useState(0);
  const [nonce, setNonce] = useState(0);
  useEffect(() => setOffset(0), [q]);

  const members = useApi<ListMembersPage>(
    (signal) => api.listListMembers(listId, { q: q || undefined, limit: PAGE_SIZE, offset }, signal),
    [listId, q, offset, nonce],
  );

  const plist = list.data;
  const people = plist?.kind === "contact";
  const rows = useMemo(() => members.data?.items ?? [], [members.data]);
  const keyOf = (m: ListMember) => (people ? m.contact_id ?? m.account_id : m.account_id);
  const visibleIds = useMemo(() => rows.map(keyOf), [rows, people]); // eslint-disable-line react-hooks/exhaustive-deps
  const selection = useSelection(visibleIds);

  const [addOpen, setAddOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [removing, setRemoving] = useState(false);

  function refresh() {
    setNonce((n) => n + 1);
    void list.refetch();
  }

  async function removeSelected() {
    if (!plist) return;
    setRemoving(true);
    try {
      const ids = people ? { contactIds: selection.ids } : { accountIds: selection.ids };
      const res = await api.removeListMembers(plist.id, ids);
      toast.success(
        `Removed ${formatNumber(res.removed)} from “${plist.name}”`,
        people ? "They stay in Contacts." : "They stay in Accounts.",
      );
      selection.clear();
      refresh();
    } catch (err) {
      toast.error("Couldn't remove them", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setRemoving(false);
    }
  }

  function startCampaign() {
    if (!plist) return;
    navigate("/engagement/campaigns/new", {
      state: { listId: plist.id, listName: plist.name, listKind: plist.kind, listMembers: plist.members },
    });
  }

  const columns: Column<ListMember>[] = useMemo(() => {
    const select: Column<ListMember> = {
      key: "select",
      width: "44px",
      align: "center",
      header: (
        <Checkbox
          label="Select everyone on this page"
          checked={selection.allVisible}
          indeterminate={selection.someVisible}
          disabled={visibleIds.length === 0}
          onChange={selection.toggleVisible}
        />
      ),
      render: (m) => (
        <Checkbox
          label={`Select ${people ? m.full_name : m.account_name}`}
          checked={selection.selected.has(keyOf(m))}
          onChange={() => selection.toggle(keyOf(m))}
        />
      ),
    };
    const company: Column<ListMember> = {
      key: "company",
      header: "Company",
      render: (m) => (
        <div className={styles.stack}>
          <Link to={`/accounts/${m.account_id}`} className={styles.link} onClick={(e) => e.stopPropagation()}>
            {m.account_name}
          </Link>
          {!people && m.domain && <span className={styles.sub}>{m.domain}</span>}
        </div>
      ),
    };
    if (people) {
      return [
        ...(plist?.can_edit ? [select] : []),
        {
          key: "person",
          header: "Person",
          render: (m) => (
            <div className={styles.stack}>
              <span className={styles.name}>{m.full_name}</span>
              {m.title && <span className={styles.sub}>{m.title}</span>}
            </div>
          ),
        },
        {
          key: "email",
          header: "Email",
          hideOnMobile: true,
          render: (m) =>
            m.email ? (
              <div className={styles.stack}>
                <span className={styles.mono}>{m.email}</span>
                {m.email_status === "invalid" && <Badge tone="warning">Address invalid</Badge>}
              </div>
            ) : (
              <span className={styles.muted}>No address, so a campaign skips them</span>
            ),
        },
        company,
      ];
    }
    return [
      ...(plist?.can_edit ? [select] : []),
      company,
      {
        key: "industry",
        header: "Industry",
        hideOnMobile: true,
        render: (m) => m.industry ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "country",
        header: "Location",
        hideOnMobile: true,
        render: (m) => m.country ?? <span className={styles.muted}>—</span>,
      },
      {
        key: "employees",
        header: "Employees",
        align: "right",
        render: (m) => formatNumber(m.employee_count),
      },
    ];
  }, [people, plist?.can_edit, selection, visibleIds]); // eslint-disable-line react-hooks/exhaustive-deps

  if (list.loading && !plist) {
    return (
      <div>
        <Skeleton width={240} height={28} />
        <Skeleton width="100%" height={320} className={styles.gap} />
      </div>
    );
  }
  if (list.error || !plist) {
    return (
      <div>
        <PageHeader
          title="List"
          eyebrow={<Link to="/lists" className={styles.back}><Icons.ChevronLeftIcon /> Lists</Link>}
        />
        {list.error?.status === 404 ? (
          <EmptyState
            icon={<Icons.ListIcon />}
            title="This list is gone"
            description="It was deleted, or it belongs to another workspace."
            action={<Link to="/lists" className={styles.buttonLink}>Back to Lists</Link>}
          />
        ) : (
          <ErrorState title="Couldn't load this list" message={list.error?.detail} onRetry={list.refetch} />
        )}
      </div>
    );
  }

  const madeBy = !plist.owner_user_id
    ? null
    : plist.owner_user_id === me
      ? "you"
      : (plist.owner_name ?? "a former member");
  const total = members.data?.total ?? 0;
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(offset + PAGE_SIZE, total);
  const addLabel = people ? "Add people" : "Add companies";

  return (
    <div>
      <PageHeader
        eyebrow={<Link to="/lists" className={styles.back}><Icons.ChevronLeftIcon /> Lists</Link>}
        title={plist.name}
        description={
          <span className={styles.meta}>
            {people ? (
              <Badge tone="info" icon={<Icons.UsersIcon />}>People</Badge>
            ) : (
              <Badge tone="neutral" icon={<Icons.BuildingIcon />}>Companies</Badge>
            )}
            <span>
              {formatNumber(plist.members)} {people ? (plist.members === 1 ? "person" : "people") : plist.members === 1 ? "company" : "companies"}
              {people && plist.members > 0 && ` at ${formatNumber(plist.accounts)} ${plist.accounts === 1 ? "company" : "companies"}`}
            </span>
            {madeBy && <span>Made by {madeBy}</span>}
            <span>Updated {timeAgo(plist.updated_at ?? plist.created_at)}</span>
          </span>
        }
        actions={
          <>
            {plist.can_edit && (
              <>
                <Button variant="ghost" onClick={() => setRenameOpen(true)}>
                  Rename
                </Button>
                <Button variant="ghost" iconLeft={<Icons.TrashIcon />} onClick={() => setDeleteOpen(true)}>
                  Delete
                </Button>
              </>
            )}
            {engineOn && (
              <Button
                iconLeft={<Icons.SendIcon />}
                onClick={startCampaign}
                disabled={plist.members === 0}
                title={plist.members === 0 ? "Add members first" : undefined}
              >
                Start a campaign
              </Button>
            )}
          </>
        }
      />

      <div className={styles.toolbar}>
        <div className={styles.search}>
          <Input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={people ? "Search name, title, email or company" : "Search name or domain"}
            iconLeft={<Icons.SearchIcon />}
            aria-label="Search this list"
          />
        </div>
        {members.loading && members.data && (
          <span className={styles.refreshing} role="status">
            <Spinner size={14} /> Updating
          </span>
        )}
        {plist.can_edit && (
          <Button variant="secondary" iconLeft={<Icons.PlusIcon />} onClick={() => setAddOpen(true)}>
            {addLabel}
          </Button>
        )}
      </div>

      {selection.selected.size > 0 && (
        <div className={styles.selectionBar} role="region" aria-label="Selection actions">
          <span className={styles.selectionCount}>{formatNumber(selection.selected.size)} selected</span>
          <div className={styles.selectionActions}>
            <Button size="sm" variant="ghost" onClick={selection.clear}>
              Clear
            </Button>
            <Button size="sm" variant="danger" iconLeft={<Icons.TrashIcon />} loading={removing} onClick={removeSelected}>
              Remove from list
            </Button>
          </div>
        </div>
      )}

      {members.error && !members.data ? (
        <ErrorState title="Couldn't load the members" message={members.error.detail} onRetry={members.refetch} />
      ) : (
        <Card padding="none">
          <DataTable<ListMember>
            columns={columns}
            rows={rows}
            getRowKey={(m) => `${m.account_id}:${m.contact_id ?? ""}`}
            loading={members.loading && !members.data}
            skeletonRows={6}
            caption={`Members of ${plist.name}`}
            empty={
              q ? (
                <EmptyState compact icon={<Icons.SearchIcon />} title="No matches" description={`Nobody on this list matches “${q}”.`} />
              ) : (
                <EmptyState
                  icon={people ? <Icons.UsersIcon /> : <Icons.BuildingIcon />}
                  title="Nobody on this list yet"
                  description={
                    people
                      ? "Add people here, or tick them on Contacts or discovery results and choose Add to list."
                      : "Add companies here, or tick them on Accounts or discovery results and choose Add to list."
                  }
                  action={
                    plist.can_edit ? (
                      <Button iconLeft={<Icons.PlusIcon />} onClick={() => setAddOpen(true)}>
                        {addLabel}
                      </Button>
                    ) : undefined
                  }
                />
              )
            }
          />
        </Card>
      )}

      {total > PAGE_SIZE && (
        <div className={styles.pager}>
          <span className={styles.range}>
            {formatNumber(start)}–{formatNumber(end)} of {formatNumber(total)}
          </span>
          <Button
            size="sm"
            variant="secondary"
            iconLeft={<Icons.ChevronLeftIcon />}
            disabled={offset === 0 || members.loading}
            onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
          >
            Previous
          </Button>
          <Button
            size="sm"
            variant="secondary"
            iconRight={<Icons.ChevronRightIcon />}
            disabled={end >= total || members.loading}
            onClick={() => setOffset((o) => o + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      )}

      {plist.can_edit && (
        <AddMembersModal
          open={addOpen}
          onClose={() => setAddOpen(false)}
          list={plist}
          onAdded={refresh}
        />
      )}
      <RenameModal
        open={renameOpen}
        list={plist}
        onClose={() => setRenameOpen(false)}
        onRenamed={() => void list.refetch()}
      />
      <DeleteModal
        open={deleteOpen}
        list={plist}
        onClose={() => setDeleteOpen(false)}
        onDeleted={() => navigate("/lists", { replace: true })}
      />
    </div>
  );
}

function RenameModal({
  open,
  list,
  onClose,
  onRenamed,
}: {
  open: boolean;
  list: ProspectList;
  onClose: () => void;
  onRenamed: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [name, setName] = useState(list.name);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) setName(list.name);
  }, [open, list.name]);

  async function save() {
    const next = name.trim();
    if (!next || next === list.name) return onClose();
    setBusy(true);
    try {
      await api.renameList(list.id, next);
      onRenamed();
      onClose();
    } catch (err) {
      toast.error("Couldn't rename the list", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="sm"
      title="Rename list"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!name.trim()}>
            Save name
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <Field label="Name" required>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoFocus />
        </Field>
      </form>
    </Modal>
  );
}

function DeleteModal({
  open,
  list,
  onClose,
  onDeleted,
}: {
  open: boolean;
  list: ProspectList;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const people = list.kind === "contact";

  async function remove() {
    setBusy(true);
    try {
      await api.deleteList(list.id);
      toast.success(`Deleted “${list.name}”`);
      onDeleted();
    } catch (err) {
      toast.error("Couldn't delete the list", err instanceof ApiError ? err.detail : "Please try again.");
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="sm"
      title={`Delete “${list.name}”?`}
      description={`The list goes away. Its ${people ? "people stay in Contacts" : "companies stay in Accounts"}, and campaigns already started from it carry on.`}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Keep it
          </Button>
          <Button variant="danger" onClick={remove} loading={busy}>
            Delete list
          </Button>
        </>
      }
    >
      <p className={styles.deleteNote}>This cannot be undone from here.</p>
    </Modal>
  );
}

export default ListDetailPage;
