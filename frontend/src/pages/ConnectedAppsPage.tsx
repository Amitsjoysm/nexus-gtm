import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Checkbox,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  Tabs,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";
import { SCOPES, SCOPE_COPY, sortScopes } from "@/components/mcp/scopes";
import { PRODUCT_NAME, SERVER_ID } from "@/lib/brand";
import type { McpConnection, McpKeyCreated, McpScope, McpSetup } from "@/lib/types";
import styles from "./ConnectedAppsPage.module.css";

/**
 * AI apps: use the product from Claude, ChatGPT, Cursor, VS Code and the model APIs (MCP).
 *
 * Every member can connect their own apps, so this page is not under Settings (admin-only). Two
 * jobs, in the order people need them: connect an app (the address and the steps for the app they
 * use), then see and control what is connected. Admins and owners also see everyone's
 * connections, because "who connected what to our CRM data" is a question an admin must be able
 * to answer and act on.
 *
 * The API key secret exists in the browser exactly once, in the create dialog, and is never
 * fetched again: the list carries only its first characters.
 */

const ADMIN_ROLES = new Set(["admin", "owner"]);

type ClientId = "claude" | "chatgpt" | "claude-code" | "editors" | "api";

const CLIENTS: { value: ClientId; label: string }[] = [
  { value: "claude", label: "Claude" },
  { value: "chatgpt", label: "ChatGPT" },
  { value: "claude-code", label: "Claude Code" },
  { value: "editors", label: "Cursor and VS Code" },
  { value: "api", label: "Model APIs" },
];

const EXPIRY_OPTIONS = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "never", label: "Never" },
];

function useCopy() {
  const toast = useToast();
  return async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${what} copied`);
    } catch {
      toast.error(`Couldn't copy the ${what.toLowerCase()}`, "Select it and copy it by hand.");
    }
  };
}

function CopyRow({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  const copy = useCopy();
  return (
    <div className={styles.copyRow}>
      <code className={mono ? styles.copyValue : styles.copyValuePlain} aria-label={label}>
        {value}
      </code>
      <Button variant="secondary" size="sm" onClick={() => copy(value, label)}>
        Copy
      </Button>
    </div>
  );
}

function Snippet({ label, code }: { label: string; code: string }) {
  const copy = useCopy();
  return (
    <div className={styles.snippet}>
      <div className={styles.snippetBar}>
        <span className={styles.snippetLabel}>{label}</span>
        <Button variant="ghost" size="sm" onClick={() => copy(code, label)}>
          Copy
        </Button>
      </div>
      <pre className={styles.snippetCode} aria-label={label}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

function Steps({ children }: { children: ReactNode[] }) {
  return (
    <ol className={styles.steps}>
      {children.map((step, i) => (
        <li key={i} className={styles.step}>
          <span className={styles.stepNumber} aria-hidden="true">
            {i + 1}
          </span>
          <div className={styles.stepBody}>{step}</div>
        </li>
      ))}
    </ol>
  );
}

function ClientGuide({ client, url }: { client: ClientId; url: string }) {
  const keyPlaceholder = "YOUR_API_KEY";
  switch (client) {
    case "claude":
      return (
        <Steps>
          {[
            <p key="a">
              In Claude, open <strong>Settings</strong>, then <strong>Connectors</strong>, and choose{" "}
              <strong>Add custom connector</strong>. Works on claude.ai, the desktop app and mobile.
            </p>,
            <p key="b">Name it {PRODUCT_NAME} and paste the server address above.</p>,
            <p key="c">
              Choose <strong>Connect</strong>. Claude opens {PRODUCT_NAME}, where you choose what it may
              do and approve. No API key is needed.
            </p>,
          ]}
        </Steps>
      );
    case "chatgpt":
      return (
        <Steps>
          {[
            <p key="a">
              In ChatGPT, open <strong>Settings</strong>, then <strong>Apps &amp; Connectors</strong>.
              Under Advanced settings, turn on <strong>Developer mode</strong> (on plans that include
              it).
            </p>,
            <p key="b">
              Choose <strong>Create</strong>, paste the server address above and pick{" "}
              <strong>OAuth</strong> as the authentication.
            </p>,
            <p key="c">ChatGPT opens {PRODUCT_NAME}, where you choose what it may do and approve.</p>,
          ]}
        </Steps>
      );
    case "claude-code":
      return (
        <Steps>
          {[
            <div key="a">
              <p>Add {PRODUCT_NAME} from a terminal:</p>
              <Snippet label="Claude Code command" code={`claude mcp add --transport http ${SERVER_ID} ${url}`} />
            </div>,
            <p key="b">
              In Claude Code, run <code className={styles.inlineCode}>/mcp</code> and choose{" "}
              {PRODUCT_NAME} to sign in. Prefer a key? Create one below and add{" "}
              <code className={styles.inlineCode}>--header "Authorization: Bearer {keyPlaceholder}"</code>{" "}
              to the command.
            </p>,
          ]}
        </Steps>
      );
    case "editors":
      return (
        <Steps>
          {[
            <div key="a">
              <p>
                Cursor: add {PRODUCT_NAME} to <code className={styles.inlineCode}>~/.cursor/mcp.json</code>.
              </p>
              <Snippet
                label="Cursor configuration"
                code={JSON.stringify({ mcpServers: { [SERVER_ID]: { url } } }, null, 2)}
              />
            </div>,
            <div key="b">
              <p>
                VS Code: add it to <code className={styles.inlineCode}>.vscode/mcp.json</code>.
              </p>
              <Snippet
                label="VS Code configuration"
                code={JSON.stringify({ servers: { [SERVER_ID]: { type: "http", url } } }, null, 2)}
              />
            </div>,
            <p key="c">Both open {PRODUCT_NAME} to sign in the first time a tool is used.</p>,
          ]}
        </Steps>
      );
    case "api":
      return (
        <Steps>
          {[
            <p key="a">Create an API key below. Give it only the permissions your script needs.</p>,
            <div key="b">
              <p>OpenAI's Responses API takes {PRODUCT_NAME} as a remote MCP tool:</p>
              <Snippet
                label="Responses API tool"
                code={JSON.stringify(
                  {
                    type: "mcp",
                    server_label: SERVER_ID,
                    server_url: url,
                    headers: { Authorization: `Bearer ${keyPlaceholder}` },
                    require_approval: "never",
                  },
                  null,
                  2,
                )}
              />
            </div>,
            <p key="c">
              Groq and xAI offer remote MCP tools too: give them the same address and key. Keep the
              key out of code you share; it acts as you.
            </p>,
          ]}
        </Steps>
      );
  }
}

function ConnectPanel({ setup }: { setup: McpSetup }) {
  const [client, setClient] = useState<ClientId>("claude");
  return (
    <section className={styles.section} aria-labelledby="connect-heading">
      <h3 id="connect-heading" className={styles.sectionTitle}>
        Connect an app
      </h3>
      <div className={styles.connect}>
        <div className={styles.address}>
          <span className={styles.addressLabel}>Server address</span>
          <CopyRow label="Server address" value={setup.server_url} />
          {!setup.public_address_set && (
            <p className={styles.addressNote} role="note">
              <Icons.AlertTriangleIcon aria-hidden="true" />
              <span>
                Claude, ChatGPT and the model APIs reach {PRODUCT_NAME} from the internet, so they need this
                deployment's public address. A platform admin sets it under Control plane,
                Configuration, App public address.
              </span>
            </p>
          )}
        </div>
        <Tabs
          items={CLIENTS}
          value={client}
          onChange={(v) => setClient(v as ClientId)}
          aria-label="Choose your app"
          idPrefix="mcp-client"
          wrap
        />
        <div
          role="tabpanel"
          id={`mcp-client-panel-${client}`}
          aria-labelledby={`mcp-client-tab-${client}`}
          className={styles.guide}
        >
          <ClientGuide client={client} url={setup.server_url} />
        </div>
      </div>
    </section>
  );
}

function ScopeBadges({ scopes }: { scopes: McpScope[] }) {
  return (
    <span className={styles.scopeList}>
      {sortScopes(scopes).map((s) => (
        <Badge key={s} tone={s === "read" ? "neutral" : s === "research" ? "info" : "accent"}>
          {SCOPE_COPY[s].label}
        </Badge>
      ))}
    </span>
  );
}

function ScopeChoices({
  value,
  onChange,
  idPrefix,
}: {
  value: McpScope[];
  onChange: (next: McpScope[]) => void;
  idPrefix: string;
}) {
  return (
    <fieldset className={styles.scopes}>
      <legend className={styles.scopesLegend}>What it may do</legend>
      {SCOPES.map((s) => {
        const id = `${idPrefix}-${s}`;
        const checked = value.includes(s);
        return (
          <label key={s} htmlFor={id} className={styles.scopeChoice}>
            <Checkbox
              id={id}
              label={SCOPE_COPY[s].label}
              checked={checked}
              onChange={(e) =>
                onChange(
                  e.target.checked ? sortScopes([...value, s]) : value.filter((x) => x !== s),
                )
              }
            />
            <span className={styles.scopeText}>
              <span className={styles.scopeName}>{SCOPE_COPY[s].label}</span>
              <span className={styles.scopeDetail}>{SCOPE_COPY[s].detail}</span>
            </span>
          </label>
        );
      })}
    </fieldset>
  );
}

function CreateKeyModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const api = useApiClient();
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<McpScope[]>(["read"]);
  const [expiry, setExpiry] = useState("90");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<McpKeyCreated | null>(null);

  const close = () => {
    setName("");
    setScopes(["read"]);
    setExpiry("90");
    setError("");
    setCreated(null);
    onClose();
  };

  const submit = async () => {
    if (!name.trim() || scopes.length === 0) return;
    setBusy(true);
    setError("");
    try {
      const res = await api.createMcpKey({
        name: name.trim(),
        scopes,
        expires_in_days: expiry === "never" ? null : Number(expiry),
      });
      setCreated(res);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Couldn't create the key.");
    } finally {
      setBusy(false);
    }
  };

  if (created) {
    return (
      <Modal
        open={open}
        onClose={close}
        title="Copy your API key"
        description="It is shown only once. Store it where your app or script keeps secrets."
        footer={
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        }
      >
        <div className={styles.secretBlock}>
          <CopyRow label="API key" value={created.secret} />
          <p className={styles.secretWarning}>
            <Icons.LockIcon aria-hidden="true" />
            <span>
              Anyone with this key can act as you, with the permissions you chose. If it leaks,
              revoke it on this page.
            </span>
          </p>
          <p className={styles.secretHint}>Claude Code, with this key:</p>
          <Snippet
            label="Claude Code command"
            code={`claude mcp add --transport http ${SERVER_ID} ${created.server_url} --header "Authorization: Bearer ${created.secret}"`}
          />
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Create an API key"
      description="For Claude Code, editors, scripts and the model APIs. Apps that sign in with OAuth don't need one."
      footer={
        <>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={submit}
            loading={busy}
            disabled={!name.trim() || scopes.length === 0}
          >
            Create key
          </Button>
        </>
      }
    >
      <form
        className={styles.form}
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Field label="Name" hint="Say where it is used, like 'Laptop' or 'Weekly report script'.">
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={80} autoFocus />
        </Field>
        <ScopeChoices value={scopes} onChange={setScopes} idPrefix="new-key" />
        <Field label="Expires">
          <Select
            value={expiry}
            onChange={(e) => setExpiry(e.target.value)}
            options={EXPIRY_OPTIONS}
          />
        </Field>
        {error && (
          <p className={styles.formError} role="alert">
            {error}
          </p>
        )}
      </form>
    </Modal>
  );
}

function EditModal({
  connection,
  onClose,
  onSaved,
}: {
  connection: McpConnection | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [scopes, setScopes] = useState<McpScope[]>([]);
  const [busy, setBusy] = useState(false);
  const [lastId, setLastId] = useState<string | null>(null);
  if (connection && connection.id !== lastId) {
    setLastId(connection.id);
    setScopes(sortScopes(connection.scopes));
  }
  const save = async () => {
    if (!connection || scopes.length === 0) return;
    setBusy(true);
    try {
      await api.updateConnection(connection.id, { scopes });
      toast.success("Permissions changed", "The app sees the change within a minute.");
      onSaved();
      onClose();
    } catch (err) {
      toast.error("Couldn't change permissions", err instanceof ApiError ? err.detail : undefined);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={connection !== null}
      onClose={onClose}
      title={connection ? `Permissions for ${connection.name}` : "Permissions"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={save} loading={busy} disabled={scopes.length === 0}>
            Save permissions
          </Button>
        </>
      }
    >
      <ScopeChoices value={scopes} onChange={setScopes} idPrefix="edit-key" />
    </Modal>
  );
}

function RevokeModal({
  connection,
  onClose,
  onRevoked,
}: {
  connection: McpConnection | null;
  onClose: () => void;
  onRevoked: () => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const revoke = async () => {
    if (!connection) return;
    setBusy(true);
    try {
      await api.revokeConnection(connection.id);
      toast.success(`${connection.name} revoked`);
      onRevoked();
      onClose();
    } catch (err) {
      toast.error("Couldn't revoke it", err instanceof ApiError ? err.detail : undefined);
    } finally {
      setBusy(false);
    }
  };
  const theirs = connection && !connection.is_mine;
  return (
    <Modal
      open={connection !== null}
      onClose={onClose}
      size="sm"
      title={connection ? `Revoke ${connection.name}?` : "Revoke"}
      description={
        theirs
          ? `It belongs to ${connection?.owner_name || connection?.owner_email}. It stops working at once and they will need to connect again.`
          : "It stops working at once. To use it again, connect again."
      }
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="danger" onClick={revoke} loading={busy}>
            Revoke access
          </Button>
        </>
      }
    >
      {null}
    </Modal>
  );
}

function ConnectionsPanel({
  isAdmin,
  onCreate,
  refreshKey,
}: {
  isAdmin: boolean;
  onCreate: () => void;
  refreshKey: number;
}) {
  const api = useApiClient();
  const [view, setView] = useState<"mine" | "team">("mine");
  const [editing, setEditing] = useState<McpConnection | null>(null);
  const [revoking, setRevoking] = useState<McpConnection | null>(null);
  const list = useApi<McpConnection[]>((signal) => api.listConnections(view, signal), [
    view,
    refreshKey,
  ]);

  const columns = useMemo<Column<McpConnection>[]>(() => {
    const cols: Column<McpConnection>[] = [
      {
        key: "name",
        header: "App or key",
        render: (c) => (
          <div className={styles.nameCell}>
            <span className={styles.nameLine}>
              <span className={styles.name}>{c.name}</span>
              {c.kind === "oauth" &&
                (c.verified ? (
                  <Badge tone="success" icon={<Icons.ShieldCheckIcon />}>
                    Verified
                  </Badge>
                ) : (
                  <Badge tone="warning">Unverified</Badge>
                ))}
            </span>
            <span className={styles.subLine}>
              {c.kind === "api_key" ? (
                <code className={styles.keyPrefix}>{c.key_prefix}…</code>
              ) : (
                <>Signed in with OAuth{c.client_host ? `, returns to ${c.client_host}` : ""}</>
              )}
            </span>
          </div>
        ),
        sortValue: (c) => c.name.toLowerCase(),
      },
      {
        key: "scopes",
        header: "Permissions",
        render: (c) => <ScopeBadges scopes={c.scopes} />,
      },
      {
        key: "last_used_at",
        header: "Last used",
        render: (c) => (
          <span title={c.last_used_at ? formatDate(c.last_used_at) : undefined}>
            {c.last_used_at ? timeAgo(c.last_used_at) : "Never"}
          </span>
        ),
        sortValue: (c) => c.last_used_at ?? "",
        hideOnMobile: true,
      },
      {
        key: "expires_at",
        header: "Expires",
        render: (c) => (c.expires_at ? formatDate(c.expires_at) : "Never"),
        hideOnMobile: true,
      },
    ];
    if (view === "team") {
      cols.splice(1, 0, {
        key: "owner",
        header: "Connected by",
        render: (c) => (
          <span className={styles.owner}>
            {c.is_mine ? "You" : c.owner_name || c.owner_email}
          </span>
        ),
        sortValue: (c) => (c.owner_name || c.owner_email).toLowerCase(),
      });
    }
    cols.push({
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      render: (c) => (
        <span className={styles.rowActions}>
          {c.is_mine && (
            <Button variant="secondary" size="sm" onClick={() => setEditing(c)}>
              Permissions
            </Button>
          )}
          <Button variant="secondary" size="sm" onClick={() => setRevoking(c)}>
            Revoke
          </Button>
        </span>
      ),
    });
    return cols;
  }, [view]);

  return (
    <section className={styles.section} aria-labelledby="connections-heading">
      <div className={styles.sectionHead}>
        <h3 id="connections-heading" className={styles.sectionTitle}>
          Connections
        </h3>
        {isAdmin && (
          <Tabs
            variant="segmented"
            items={[
              { value: "mine", label: "Mine" },
              { value: "team", label: "Everyone" },
            ]}
            value={view}
            onChange={(v) => setView(v as "mine" | "team")}
            aria-label="Whose connections"
            className={styles.viewSwitch}
          />
        )}
      </div>
      {list.error && !list.data ? (
        <ErrorState message={list.error.detail} onRetry={list.refetch} />
      ) : (
        <DataTable
          columns={columns}
          rows={list.data ?? []}
          getRowKey={(c) => c.id}
          loading={list.loading && !list.data}
          skeletonRows={3}
          caption={view === "team" ? "Every connected app in the workspace" : "Your connected apps"}
          minWidth={720}
          empty={
            <EmptyState
              icon={<Icons.PlugIcon />}
              title={view === "team" ? "Nobody has connected an app" : "No apps connected yet"}
              description={
                view === "team"
                  ? "Connections members make appear here, and you can revoke any of them."
                  : "Connect Claude or ChatGPT with the steps above, or create an API key for a script or editor."
              }
              action={
                view === "mine" ? (
                  <Button variant="primary" iconLeft={<Icons.PlusIcon />} onClick={onCreate}>
                    Create API key
                  </Button>
                ) : undefined
              }
            />
          }
        />
      )}
      <EditModal connection={editing} onClose={() => setEditing(null)} onSaved={list.refetch} />
      <RevokeModal
        connection={revoking}
        onClose={() => setRevoking(null)}
        onRevoked={list.refetch}
      />
    </section>
  );
}

export function ConnectedAppsPage() {
  const api = useApiClient();
  const { session } = useAuth();
  const isAdmin = ADMIN_ROLES.has(session?.role ?? "");
  const setup = useApi<McpSetup>((signal) => api.mcpSetup(signal), []);
  const [creating, setCreating] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  return (
    <div>
      <PageHeader
        title="AI apps"
        description={`Use ${PRODUCT_NAME} from Claude, ChatGPT and other AI assistants. Each connection acts as you, with only the permissions you give it, and can be revoked here at any time.`}
        actions={
          <Button variant="primary" iconLeft={<Icons.PlusIcon />} onClick={() => setCreating(true)}>
            Create API key
          </Button>
        }
      />
      <div className={styles.stack}>
        {setup.error && !setup.data ? (
          <ErrorState message={setup.error.detail} onRetry={setup.refetch} />
        ) : setup.data ? (
          <ConnectPanel setup={setup.data} />
        ) : (
          <div className={styles.section} aria-busy="true">
            <Skeleton width="10rem" height="1.25rem" />
            <Skeleton height="2.75rem" />
            <Skeleton height="9rem" />
          </div>
        )}
        <ConnectionsPanel
          isAdmin={isAdmin}
          onCreate={() => setCreating(true)}
          refreshKey={refreshKey}
        />
      </div>
      <CreateKeyModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={() => setRefreshKey((n) => n + 1)}
      />
    </div>
  );
}

export default ConnectedAppsPage;
