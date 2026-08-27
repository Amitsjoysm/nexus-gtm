import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { RecordImportModal } from "@/components/imports/RecordImportModal";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  Spinner,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { ProviderIcon } from "@/components/ui/providerIcons";
import { CallConsole } from "@/components/CallConsole";
import { EmailComposer } from "@/components/EmailComposer";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { strengthMeta } from "@/lib/display";
import { timeAgo } from "@/lib/format";
import type { CallTask, ContactLookalike, WorkspaceContact } from "@/lib/types";
import styles from "./ContactsPage.module.css";

const ALL = "__all__";

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  valid: "success",
  risky: "warning",
  unknown: "neutral",
  invalid: "danger",
};

// Detected email service provider (from MX records) → a friendly label shown under the address.
const PROVIDER_LABELS: Record<string, string> = {
  gsuite: "Google",
  office365: "Microsoft",
  outlook: "Outlook",
  yahoo: "Yahoo",
  zoho: "Zoho",
  proton: "Proton",
  custom: "Custom",
  disposable: "Disposable",
};
// Full name for the hover tooltip (the badge stays short to keep the column narrow).
const PROVIDER_FULL: Record<string, string> = {
  gsuite: "Google Workspace",
  office365: "Microsoft 365",
  custom: "Custom / self-hosted",
};
const providerFull = (p?: string | null) =>
  (p ? PROVIDER_FULL[p] ?? PROVIDER_LABELS[p] ?? p : "");
// The badge used to be a brand-coloured letter (G, M, O, …). A letter has to be READ, and at 9px
// a Microsoft "M" sat one glyph away from a Gmail "G" — so the one question an SDR actually asks
// of this column ("Google shop or Microsoft shop?") needed a second look every time. Real marks
// are recognised in peripheral vision instead. See components/ui/providerIcons.tsx.

// Mirrors NEXUS_EMAIL_REVERIFY_COOLDOWN_DAYS: a confirmed-valid address is re-checkable only
// after this window (the backend enforces it with a 429; this just saves the click).
const REVERIFY_COOLDOWN_DAYS = 30;
const verifiedFresh = (c: WorkspaceContact): boolean =>
  c.email_status === "valid" &&
  !!c.email_checked_at &&
  Date.now() - Date.parse(c.email_checked_at) < REVERIFY_COOLDOWN_DAYS * 86_400_000;
const reverifyDueDate = (c: WorkspaceContact): string =>
  new Date(
    Date.parse(c.email_checked_at as string) + REVERIFY_COOLDOWN_DAYS * 86_400_000,
  ).toLocaleDateString();

export function ContactsPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const toast = useToast();
  const contacts = useApi<WorkspaceContact[]>((signal) => api.listWorkspaceContacts(undefined, signal), []);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState(ALL);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reverifying, setReverifying] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [phoneId, setPhoneId] = useState<string | null>(null);
  const [callTask, setCallTask] = useState<CallTask | null>(null);
  const [emailFor, setEmailFor] = useState<WorkspaceContact | null>(null);
  const [callingId, setCallingId] = useState<string | null>(null);
  // "Similar people" — opens a modal ranking the workspace's other contacts against this one.
  const [similarFor, setSimilarFor] = useState<WorkspaceContact | null>(null);
  const [similarLoading, setSimilarLoading] = useState(false);
  const [similarData, setSimilarData] = useState<ContactLookalike[] | null>(null);
  const [similarId, setSimilarId] = useState<string | null>(null);

  async function findSimilar(c: WorkspaceContact) {
    setSimilarId(c.id);
    setSimilarFor(c);
    setSimilarData(null);
    setSimilarLoading(true);
    try {
      const res = await api.findContactLookalikes(c.id, 10);
      setSimilarData(res.lookalikes);
    } catch (err) {
      setSimilarFor(null);
      toast.error("Couldn't find similar people", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setSimilarLoading(false);
      setSimilarId(null);
    }
  }

  async function startCall(c: WorkspaceContact) {
    setCallingId(c.id);
    try {
      setCallTask(
        await api.createCallTask({
          account_id: c.account_id,
          contact_id: c.id,
          reason: `Outbound call · ${c.account_name}`,
        }),
      );
    } catch (err) {
      toast.error("Couldn't start call", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setCallingId(null);
    }
  }

  const rows = contacts.data ?? [];
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((c) => {
      if (q && ![c.full_name, c.title, c.email, c.account_name].some((v) => (v ?? "").toLowerCase().includes(q)))
        return false;
      if (statusFilter !== ALL && (c.email_status ?? "none") !== statusFilter) return false;
      return true;
    });
  }, [rows, query, statusFilter]);

  async function reverifyAll() {
    setReverifying(true);
    try {
      const res = await api.reverifyContacts(true);
      const verified = res.statuses.valid ?? 0;
      toast.success(
        "Re-verification complete",
        `Checked ${res.checked}, updated ${res.updated}${verified ? ` · ${verified} valid` : ""}.`,
      );
      contacts.refetch();
    } catch (err) {
      toast.error("Re-verify failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setReverifying(false);
    }
  }

  const unverifiedCount = useMemo(
    () => rows.filter((c) => c.email && (!c.email_status || c.email_status === "unknown")).length,
    [rows],
  );

  async function verify(c: WorkspaceContact) {
    setBusyId(c.id);
    try {
      await api.enrichContact(c.id);
      toast.success("Verifying…", `Re-checked ${c.full_name}'s email.`);
      contacts.refetch();
    } catch (err) {
      toast.error("Verify failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  /**
   * Find a phone number for one contact, on demand.
   *
   * One contact at a time and only when clicked: each lookup is a paid actor run, so a "enrich all"
   * button over a 1,000-contact list is a four-figure bill one mis-click away. If another workspace
   * already bought this number the answer is instant and costs us nothing, which the rep never has
   * to know about.
   */
  async function enrichPhone(c: WorkspaceContact) {
    setPhoneId(c.id);
    try {
      const res = await api.enrichContactPhone(c.id);
      if (res.phone || res.raw) {
        toast.success(`Found ${res.phone || res.raw}`, `${c.full_name} · phone number added.`);
        contacts.refetch();
      } else {
        // "We looked and there is nothing" is a real answer and worth stating plainly, so nobody
        // clicks it again expecting a different result.
        toast.toast({
          tone: "info",
          title: "No phone number found",
          description: `Nothing published for ${c.full_name}. We won't re-check for a while.`,
        });
      }
    } catch (err) {
      toast.error("Couldn't look up a phone", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setPhoneId(null);
    }
  }

  /**
   * Soft delete, with undo on the confirmation.
   *
   * The row survives because cadence enrolments, call activity and campaign sends all point at it;
   * a hard delete would break the record of what was actually sent to whom.
   */
  async function removeContact(c: WorkspaceContact) {
    setBusyId(c.id);
    try {
      await api.deleteContact(c.id);
      contacts.refetch();
      toast.toast({
        tone: "success",
        title: "Contact deleted",
        description: `${c.full_name} is hidden from your list. Their history is kept.`,
        action: {
          label: "Undo",
          onClick: async () => {
            try {
              await api.restoreContact(c.id);
              contacts.refetch();
            } catch (err) {
              toast.error(
                "Couldn't restore",
                err instanceof ApiError ? err.detail : "Try again.",
              );
            }
          },
        },
      });
    } catch (err) {
      toast.error("Couldn't delete", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  /** Exports what the search box is currently filtering to, not the whole workspace. */
  async function exportContacts() {
    setExporting(true);
    try {
      await api.exportContacts({ q: query.trim() || undefined });
    } catch (err) {
      toast.error("Couldn't export", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setExporting(false);
    }
  }

  const columns: Column<WorkspaceContact>[] = useMemo(
    () => [
      {
        key: "full_name",
        header: "Name",
        sortValue: (c) => c.full_name,
        render: (c) => (
          <span className={styles.name} title={c.full_name}>
            {c.full_name}
          </span>
        ),
      },
      {
        key: "title",
        header: "Title",
        sortValue: (c) => c.title,
        render: (c) =>
          c.title ? (
            <span className={styles.truncate} title={c.title}>
              {c.title}
            </span>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "account_name",
        header: "Account",
        sortValue: (c) => c.account_name,
        render: (c) => (
          <span className={styles.account} title={c.account_name}>
            {c.account_name}
          </span>
        ),
      },
      {
        key: "email",
        header: "Email",
        hideOnMobile: true,
        sortValue: (c) => c.email,
        render: (c) =>
          c.email ? (
            <div className={styles.emailCell}>
              {c.email_provider && (
                <span
                  className={styles.provider}
                  data-provider={c.email_provider}
                  title={`Mailbox provider: ${providerFull(c.email_provider)}`}
                  role="img"
                  aria-label={`Mailbox provider: ${providerFull(c.email_provider)}`}
                >
                  <ProviderIcon provider={c.email_provider} />
                </span>
              )}
              <span className={styles.mono} title={c.email}>
                {c.email}
              </span>
            </div>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "phone",
        header: "Phone",
        hideOnMobile: true,
        sortValue: (c) => c.phone,
        render: (c) =>
          c.phone ? (
            <a
              href={`tel:${c.phone.replace(/[^\d+]/g, "")}`}
              className={styles.mono}
              onClick={(e) => e.stopPropagation()}
            >
              {c.phone}
            </a>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "email_status",
        header: "Status",
        sortValue: (c) => c.email_status,
        render: (c) =>
          c.email_status ? (
            <Badge tone={STATUS_TONE[c.email_status] ?? "neutral"} dot>
              {c.email_status}
            </Badge>
          ) : c.email ? (
            // Has an address but no verdict yet — say so (and offer "Verify"), don't show a blank.
            <Badge tone="neutral" dot>
              unverified
            </Badge>
          ) : (
            <span className={styles.muted}>no email</span>
          ),
      },
      {
        key: "email_checked_at",
        header: "Checked",
        hideOnMobile: true,
        sortValue: (c) => c.email_checked_at,
        render: (c) =>
          c.email_checked_at ? (
            <span className={styles.muted} title={new Date(c.email_checked_at).toLocaleString()}>
              {timeAgo(c.email_checked_at)}
            </span>
          ) : (
            <span className={styles.muted}>never</span>
          ),
      },
      {
        key: "linkedin_url",
        header: "LinkedIn",
        hideOnMobile: true,
        align: "center",
        render: (c) =>
          c.linkedin_url ? (
            <a
              href={c.linkedin_url}
              target="_blank"
              rel="noreferrer noopener"
              className={styles.linkedin}
              title={`Open ${c.full_name}'s LinkedIn`}
              aria-label={`Open ${c.full_name}'s LinkedIn profile`}
              onClick={(e) => e.stopPropagation()}
            >
              in
            </a>
          ) : (
            <span className={styles.muted}>—</span>
          ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        render: (c) => (
          <span className={styles.rowActions} onClick={(e) => e.stopPropagation()}>
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.ShieldCheckIcon />}
              loading={busyId === c.id}
              disabled={verifiedFresh(c)}
              title={
                verifiedFresh(c)
                  ? `Verified valid ${timeAgo(c.email_checked_at as string)} — re-verification opens ${reverifyDueDate(c)}`
                  : "Verify email"
              }
              onClick={() => verify(c)}
              aria-label={`Verify ${c.full_name}'s email`}
            />
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.UsersIcon />}
              loading={similarId === c.id}
              title="Find similar people"
              onClick={() => findSimilar(c)}
              aria-label={`Find people similar to ${c.full_name}`}
            />
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.MailIcon />}
              title="Draft a personalized email"
              onClick={() => setEmailFor(c)}
              aria-label={`Draft an email to ${c.full_name}`}
            />
            <Button
              size="sm"
              variant="secondary"
              iconLeft={<Icons.PhoneIcon />}
              loading={callingId === c.id}
              title="Call"
              onClick={() => startCall(c)}
              aria-label={`Call ${c.full_name}`}
            />
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.PhoneSearchIcon />}
              loading={phoneId === c.id}
              disabled={Boolean(c.phone)}
              title={c.phone ? `Already has ${c.phone}` : "Find a phone number"}
              onClick={() => enrichPhone(c)}
              aria-label={`Find a phone number for ${c.full_name}`}
            />
            <Button
              size="sm"
              variant="ghost"
              iconLeft={<Icons.TrashIcon />}
              loading={busyId === c.id}
              title="Delete contact"
              onClick={() => removeContact(c)}
              aria-label={`Delete ${c.full_name}`}
            />
          </span>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busyId, callingId, similarId, phoneId],
  );

  return (
    <div>
      <PageHeader
        title="Contacts"
        description="Every person across this workspace. Click a row to open their account."
        actions={
          <>
            <Button
              variant="secondary"
              iconLeft={<Icons.UploadIcon />}
              onClick={() => setImportOpen(true)}
            >
              Import
            </Button>
            <Button
              variant="secondary"
              iconLeft={<Icons.DownloadIcon />}
              onClick={exportContacts}
              loading={exporting}
            >
              Export CSV
            </Button>
          </>
        }
      />

      <RecordImportModal
        open={importOpen}
        entity="contacts"
        onClose={() => setImportOpen(false)}
        onImported={() => void contacts.refetch()}
      />

      <div className={styles.toolbar}>
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search name, title, email, or account…"
          iconLeft={<Icons.SearchIcon />}
          aria-label="Search contacts"
        />
        <Select
          aria-label="Email status"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          options={[
            { value: ALL, label: "Any status" },
            { value: "valid", label: "Valid" },
            { value: "risky", label: "Risky" },
            { value: "unknown", label: "Unknown" },
            { value: "invalid", label: "Invalid" },
            { value: "none", label: "Unverified" },
          ]}
        />
        <span className={styles.count}>
          {visible.length} of {rows.length}
        </span>
        <Button
          size="sm"
          variant="secondary"
          loading={reverifying}
          disabled={unverifiedCount === 0}
          onClick={reverifyAll}
          title={
            unverifiedCount === 0
              ? "Every contact with an email already has a verdict"
              : `Re-check ${unverifiedCount} address${unverifiedCount === 1 ? "" : "es"} against the verifier`
          }
        >
          Re-verify {unverifiedCount > 0 ? `(${unverifiedCount})` : "emails"}
        </Button>
      </div>

      {contacts.error && !contacts.data ? (
        <ErrorState title="Couldn't load contacts" message={contacts.error.detail} onRetry={contacts.refetch} />
      ) : (
        <DataTable<WorkspaceContact>
          columns={columns}
          rows={visible}
          density="compact"
          getRowKey={(c) => c.id}
          loading={contacts.loading && !contacts.data}
          skeletonRows={6}
          onRowClick={(c) => navigate(`/accounts/${c.account_id}`)}
          caption="Workspace contacts"
          empty={
            <EmptyState
              icon={<Icons.UsersIcon />}
              title="No contacts yet"
              description="Run a contact discovery (Orchestrator → find contacts) or 'Find contacts' on an account to source real people."
            />
          }
        />
      )}

      {contacts.loading && !contacts.data && <Skeleton width="100%" height={48} />}

      <Modal
        open={!!callTask}
        onClose={() => setCallTask(null)}
        title="Call"
        description="AI script, click-to-dial, and one-tap outcome logging."
      >
        {callTask && (
          <CallConsole task={callTask} autoGenerate onLogged={() => setCallTask(null)} />
        )}
      </Modal>

      <Modal
        open={!!emailFor}
        onClose={() => setEmailFor(null)}
        title={emailFor ? `Email ${emailFor.full_name}` : "Email"}
        description="Hyper-personalized to this contact and account — edit, copy, or open in your mail client."
      >
        {emailFor && (
          <EmailComposer
            accountId={emailFor.account_id}
            contactId={emailFor.id}
            contactName={emailFor.full_name}
            contactEmail={emailFor.email}
          />
        )}
      </Modal>

      <Modal
        open={!!similarFor}
        onClose={() => setSimilarFor(null)}
        title={similarFor ? `People like ${similarFor.full_name}` : "Similar people"}
        description="Ranked by role, seniority, function, and how similar their company is."
      >
        {similarLoading ? (
          <div className={styles.similarLoading}>
            <Spinner size={16} /> Finding similar people…
          </div>
        ) : similarData && similarData.length > 0 ? (
          <ul className={styles.similarList}>
            {similarData.map((p) => {
              const meta = strengthMeta(p.score / 100);
              return (
                <li key={p.contact_id} className={styles.similarItem}>
                  <button
                    type="button"
                    className={styles.similarRow}
                    onClick={() => {
                      setSimilarFor(null);
                      navigate(`/accounts/${p.account_id}`);
                    }}
                  >
                    <span className={styles.similarMain}>
                      <span className={styles.name}>{p.full_name}</span>
                      <span className={styles.muted}>
                        {[p.title, p.account_name].filter(Boolean).join(" · ") || "—"}
                      </span>
                      {p.reasons.length > 0 && (
                        <span className={styles.similarWhy}>{p.reasons.join(" · ")}</span>
                      )}
                    </span>
                    <Badge tone={meta.tone} dot>
                      Match {p.score}
                    </Badge>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState
            compact
            icon={<Icons.UsersIcon />}
            title="No similar people yet"
            description="Add more contacts to your workspace to compare against."
          />
        )}
      </Modal>
    </div>
  );
}

export default ContactsPage;
