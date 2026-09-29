import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
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
  TabPanel,
  Tabs,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { FilterBuilder } from "@/components/lists/FilterBuilder";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useCurrentUserId } from "@/app/useCurrentUserId";
import { ApiError } from "@/lib/api";
import { formatNumber, timeAgo } from "@/lib/format";
import type { ListKind, ProspectList } from "@/lib/types";
import styles from "./ListsPage.module.css";

type View = "lists" | "filters";
type KindFilter = "all" | ListKind;

/**
 * Lists: companies or people you want to work together, and the start of a campaign.
 *
 * Two ways in. "New list" makes an empty one to fill from Accounts, Contacts or discovery results
 * (tick rows, "Add to list"); "Build from filters" makes an account list from firmographics and fit.
 */
export function ListsPage() {
  const navigate = useNavigate();
  const [view, setView] = useState<View>("lists");
  const [nonce, setNonce] = useState(0);
  const [newOpen, setNewOpen] = useState(false);

  return (
    <div>
      <PageHeader
        title="Lists"
        description="Companies or people you want to work together. Start a campaign from any list."
        actions={
          <Button iconLeft={<Icons.PlusIcon />} onClick={() => setNewOpen(true)}>
            New list
          </Button>
        }
      />

      <Tabs
        idPrefix="lists"
        aria-label="Lists"
        value={view}
        onChange={(v) => setView(v as View)}
        items={[
          { value: "lists", label: "Your lists" },
          { value: "filters", label: "Build from filters" },
        ]}
        className={styles.tabs}
      />

      <TabPanel id="lists-panel-lists" active={view === "lists"}>
        <SavedLists refreshKey={nonce} onNew={() => setNewOpen(true)} />
      </TabPanel>
      <TabPanel id="lists-panel-filters" active={view === "filters"}>
        <FilterBuilder
          onSaved={(built) => {
            setNonce((n) => n + 1);
            navigate(`/lists/${built.id}`);
          }}
        />
      </TabPanel>

      <NewListModal
        open={newOpen}
        onClose={() => setNewOpen(false)}
        onCreated={(id) => navigate(`/lists/${id}`)}
      />
    </div>
  );
}

function SavedLists({ refreshKey, onNew }: { refreshKey: number; onNew: () => void }) {
  const api = useApiClient();
  const navigate = useNavigate();
  const me = useCurrentUserId();
  const lists = useApi<ProspectList[]>((signal) => api.listSavedLists(signal), [refreshKey]);
  const [kind, setKind] = useState<KindFilter>("all");

  const columns: Column<ProspectList>[] = useMemo(
    () => [
      {
        key: "name",
        header: "List",
        sortValue: (l) => l.name,
        render: (l) => <span className={styles.name}>{l.name}</span>,
      },
      {
        key: "kind",
        header: "Holds",
        width: "120px",
        render: (l) =>
          l.kind === "contact" ? (
            <Badge tone="info" icon={<Icons.UsersIcon />}>People</Badge>
          ) : (
            <Badge tone="neutral" icon={<Icons.BuildingIcon />}>Companies</Badge>
          ),
      },
      {
        key: "members",
        header: "Members",
        align: "right",
        sortValue: (l) => l.members,
        render: (l) => (
          <span className={styles.count}>
            {formatNumber(l.members)}
            {l.kind === "contact" && l.members > 0 && (
              <span className={styles.sub}>
                {" "}at {formatNumber(l.accounts)} {l.accounts === 1 ? "company" : "companies"}
              </span>
            )}
          </span>
        ),
      },
      {
        key: "owner",
        header: "Made by",
        hideOnMobile: true,
        sortValue: (l) => l.owner_name ?? "",
        render: (l) =>
          !l.owner_user_id ? (
            <span className={styles.muted}>Not recorded</span>
          ) : l.owner_user_id === me ? (
            "You"
          ) : (
            (l.owner_name ?? <span className={styles.muted}>Former member</span>)
          ),
      },
      {
        key: "updated",
        header: "Updated",
        hideOnMobile: true,
        sortValue: (l) => l.updated_at ?? l.created_at,
        render: (l) => <span className={styles.muted}>{timeAgo(l.updated_at ?? l.created_at)}</span>,
      },
    ],
    [me],
  );

  return (
    <DataState
      state={lists}
      errorTitle="Couldn't load your lists"
      skeleton={
        <Card padding="none">
          <DataTable<ProspectList> columns={columns} rows={[]} getRowKey={(l) => l.id} loading />
        </Card>
      }
      isEmpty={(rows) => rows.length === 0}
      empty={
        <EmptyState
          icon={<Icons.ListIcon />}
          title="No lists yet"
          description="Make one here, or tick rows on Accounts, Contacts or discovery results and choose Add to list."
          action={
            <Button iconLeft={<Icons.PlusIcon />} onClick={onNew}>
              New list
            </Button>
          }
        />
      }
    >
      {(rows) => {
        const counts = {
          all: rows.length,
          account: rows.filter((l) => l.kind !== "contact").length,
          contact: rows.filter((l) => l.kind === "contact").length,
        };
        const visible = kind === "all" ? rows : rows.filter((l) => (l.kind ?? "account") === kind);
        return (
          <>
            <div className={styles.toolbar}>
              <Tabs
                variant="segmented"
                aria-label="Show lists of"
                value={kind}
                onChange={(v) => setKind(v as KindFilter)}
                items={[
                  { value: "all", label: "All", count: counts.all },
                  { value: "account", label: "Companies", count: counts.account },
                  { value: "contact", label: "People", count: counts.contact },
                ]}
                className={styles.kindSwitch}
              />
            </div>
            <Card padding="none">
              <DataTable<ProspectList>
                columns={columns}
                rows={visible}
                getRowKey={(l) => l.id}
                onRowClick={(l) => navigate(`/lists/${l.id}`)}
                caption="Your lists"
                empty={
                  <EmptyState
                    compact
                    icon={<Icons.ListIcon />}
                    title={kind === "contact" ? "No lists of people yet" : "No lists of companies yet"}
                    description="New list makes one; you choose whether it holds companies or people."
                  />
                }
              />
            </Card>
          </>
        );
      }}
    </DataState>
  );
}

function NewListModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ListKind>("account");
  const [busy, setBusy] = useState(false);

  async function create() {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const made = await api.createList(name.trim(), kind);
      setName("");
      onClose();
      onCreated(made.id);
    } catch (err) {
      toast.error("Couldn't create the list", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="sm"
      title="New list"
      description="You add members next, here or from Accounts, Contacts and discovery results."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={create} loading={busy} disabled={!name.trim()}>
            Create list
          </Button>
        </>
      }
    >
      <form
        className={styles.newForm}
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
      >
        <Field label="Name" required>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={200}
            placeholder="Q4 fintech targets"
            autoFocus
          />
        </Field>
        <fieldset className={styles.kinds}>
          <legend className={styles.legend}>It holds</legend>
          <label className={styles.kindOption}>
            <input
              type="radio"
              name="new-list-kind"
              checked={kind === "account"}
              onChange={() => setKind("account")}
            />
            <span>
              <span className={styles.kindTitle}>Companies</span>
              <span className={styles.kindHint}>
                A campaign emails everyone with an address at each company.
              </span>
            </span>
          </label>
          <label className={styles.kindOption}>
            <input
              type="radio"
              name="new-list-kind"
              checked={kind === "contact"}
              onChange={() => setKind("contact")}
            />
            <span>
              <span className={styles.kindTitle}>People</span>
              <span className={styles.kindHint}>A campaign emails exactly the people on it.</span>
            </span>
          </label>
        </fieldset>
      </form>
    </Modal>
  );
}

export default ListsPage;
