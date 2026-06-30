import { useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Icons,
  Input,
  Modal,
  Skeleton,
  Spinner,
  useToast,
} from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { strengthMeta } from "@/lib/display";
import type {
  Member,
  NetworkAccount,
  NetworkImportIdentity,
  NetworkImportTouchpoint,
  NetworkIntroPath,
  NetworkPersonSummary,
  NetworkSearchHit,
} from "@/lib/types";
import styles from "./NetworkPage.module.css";

/** Providers a member can connect. Real OAuth lands in a later phase; "Demo network" seeds
 *  the offline graph so the flow is reviewable end to end today. */
const PROVIDERS: { id: string; label: string }[] = [
  { id: "google", label: "Google" },
  { id: "microsoft", label: "Microsoft" },
  { id: "linkedin", label: "LinkedIn" },
  { id: "fixture", label: "Demo network" },
];

const EXAMPLE_QUERIES = [
  "CTO at a healthcare startup",
  "VP of Procurement in retail",
  "CFO at a fintech",
  "Head of Talent",
];

const RELATION_LABEL: Record<string, string> = {
  email: "email thread",
  calendar: "met in person",
  linkedin_1st: "1st-degree connection",
  follower: "follows them",
  contact: "in contacts",
};

/** A believable sample network so search + strength + intro paths are demonstrable offline. */
const DAYS_AGO = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();
const SAMPLE_IDENTITIES: NetworkImportIdentity[] = [
  { external_id: "s1", email: "ada@helixhealth.com", name: "Ada Okafor", title: "CTO", company: "Helix Health" },
  { external_id: "s2", email: "ben@nimbusrx.com", name: "Ben Carter", title: "VP Engineering", company: "Nimbus Rx" },
  { external_id: "s3", email: "carla@northstarretail.com", name: "Carla Diaz", title: "VP Procurement", company: "Northstar Retail" },
  { external_id: "s4", email: "deepa@ledgerpay.com", name: "Deepa Rao", title: "CFO", company: "LedgerPay" },
  { external_id: "s5", email: "evan@brightpath.io", name: "Evan Lee", title: "Head of Talent", company: "BrightPath" },
  { external_id: "s6", email: "farah@helixhealth.com", name: "Farah Nasser", title: "Director of Engineering", company: "Helix Health" },
];
const SAMPLE_TOUCHPOINTS: NetworkImportTouchpoint[] = [
  // Ada: recent two-way thread → strong path.
  { person_external_id: "s1", kind: "email_sent", at: DAYS_AGO(4) },
  { person_external_id: "s1", kind: "email_received", at: DAYS_AGO(3) },
  { person_external_id: "s1", kind: "meeting", at: DAYS_AGO(10) },
  // Carla: one recent send → medium.
  { person_external_id: "s3", kind: "email_sent", at: DAYS_AGO(20) },
  // Deepa: an older meeting → medium-low.
  { person_external_id: "s4", kind: "meeting", at: DAYS_AGO(120) },
];

export function NetworkPage() {
  const api = useApiClient();
  const toast = useToast();

  const sources = useApi<NetworkAccount[]>((signal) => api.listNetworkAccounts(signal), []);
  const members = useApi<Member[]>((signal) => api.listMembers(signal), []);
  const memberName = (id: string) =>
    members.data?.find((m) => m.membership_id === id)?.full_name ?? "a teammate";

  // --- search (user-triggered, owns its own async state) ---
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [results, setResults] = useState<NetworkSearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  async function runSearch(raw: string) {
    const term = raw.trim();
    if (!term) return;
    setQuery(term);
    setSubmitted(term);
    setSearching(true);
    setSearchError(null);
    try {
      setResults(await api.searchNetwork(term, 25));
    } catch (err) {
      setResults(null);
      setSearchError(err instanceof ApiError ? err.detail : "Search failed. Try again.");
    } finally {
      setSearching(false);
    }
  }

  // --- per-source actions ---
  const [connecting, setConnecting] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function connect(provider: string, label: string) {
    setConnecting(provider);
    try {
      await api.connectNetworkAccount({
        provider,
        external_account_id: `${provider}-${Math.random().toString(36).slice(2, 9)}`,
        display_email: label === "Demo network" ? "Demo network" : `${label} account`,
      });
      sources.refetch();
      toast.success("Source connected", `${label} is ready. Import or sync to fill the graph.`);
    } catch (err) {
      toast.error("Couldn't connect", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setConnecting(null);
    }
  }

  async function togglePooling(acc: NetworkAccount) {
    const next = !acc.pooling_enabled;
    sources.setData((prev) =>
      (prev ?? []).map((a) => (a.id === acc.id ? { ...a, pooling_enabled: next } : a)),
    );
    try {
      await api.patchNetworkAccount(acc.id, { pooling_enabled: next });
      toast.success(
        next ? "Shared with your team" : "Set to private",
        next
          ? "Teammates can now find warm intros through this network."
          : "Only you can see relationships from this source.",
      );
    } catch (err) {
      sources.setData((prev) =>
        (prev ?? []).map((a) => (a.id === acc.id ? { ...a, pooling_enabled: !next } : a)),
      );
      toast.error("Couldn't update pooling", err instanceof ApiError ? err.detail : "Try again.");
    }
  }

  async function importSample(acc: NetworkAccount) {
    setBusyId(acc.id);
    try {
      const res = await api.importNetworkBatch(acc.id, {
        identities: SAMPLE_IDENTITIES,
        touchpoints: SAMPLE_TOUCHPOINTS,
      });
      toast.success(
        "Sample contacts imported",
        `Added ${res.new_persons} people. Try searching "CTO at a healthcare startup".`,
      );
      if (submitted) runSearch(submitted);
    } catch (err) {
      toast.error("Import failed", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  // --- intro-path drawer ---
  const [activePerson, setActivePerson] = useState<NetworkPersonSummary | null>(null);
  const [paths, setPaths] = useState<NetworkIntroPath[] | null>(null);
  const [pathsLoading, setPathsLoading] = useState(false);

  async function openPerson(person: NetworkPersonSummary) {
    setActivePerson(person);
    setPaths(null);
    setPathsLoading(true);
    try {
      setPaths(await api.networkIntroPaths(person.id));
    } catch (err) {
      toast.error("Couldn't load intro paths", err instanceof ApiError ? err.detail : "Try again.");
      setActivePerson(null);
    } finally {
      setPathsLoading(false);
    }
  }

  const srcRows = sources.data ?? [];
  const hasSources = srcRows.length > 0;

  return (
    <div>
      <PageHeader
        title="Network"
        description="Search the people your team already knows, and find the warmest path to any buyer."
      />

      {/* ---- search ---- */}
      <div className={styles.searchCard}>
        <form
          className={styles.searchForm}
          onSubmit={(e) => {
            e.preventDefault();
            runSearch(query);
          }}
        >
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Who do we know who's a CTO at a healthcare startup?"
            iconLeft={<Icons.SearchIcon />}
            aria-label="Search your network in plain language"
          />
          <Button type="submit" loading={searching} iconLeft={<Icons.SparklesIcon />}>
            Search
          </Button>
        </form>
        <div className={styles.examples}>
          <span className={styles.examplesLabel}>Try:</span>
          {EXAMPLE_QUERIES.map((q) => (
            <button key={q} type="button" className={styles.chip} onClick={() => runSearch(q)}>
              {q}
            </button>
          ))}
        </div>
      </div>

      {/* ---- body: results + sources ---- */}
      <div className={styles.layout}>
        <section aria-label="Search results">
          <h2 className={styles.sectionTitle}>
            {submitted ? `Results for "${submitted}"` : "Results"}
          </h2>

          {searching ? (
            <div className={styles.results}>
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} width="100%" height={64} />
              ))}
            </div>
          ) : searchError ? (
            <ErrorState
              title="Search failed"
              message={searchError}
              onRetry={() => submitted && runSearch(submitted)}
            />
          ) : !submitted ? (
            <EmptyState
              icon={<Icons.SearchIcon />}
              title="Ask in plain language"
              description="Describe who you're trying to reach. We rank the people your team already knows by how well they match and how strong the relationship is."
            />
          ) : results && results.length > 0 ? (
            <div className={styles.results}>
              {results.map((hit, i) => {
                const meta = strengthMeta(hit.best_strength / 100);
                const brokers = hit.broker_member_ids;
                const sub = [hit.person.title, hit.person.company].filter(Boolean).join(" · ");
                return (
                  <button
                    key={hit.person.id}
                    type="button"
                    className={`${styles.resultRow} ${styles.rise}`}
                    style={{ animationDelay: `${Math.min(i, 8) * 28}ms` }}
                    onClick={() => openPerson(hit.person)}
                    aria-label={`See intro paths to ${hit.person.full_name}`}
                  >
                    <span className={styles.resultMain}>
                      <span className={styles.name}>{hit.person.full_name}</span>
                      {sub && <span className={styles.sub}>{sub}</span>}
                      {hit.person.primary_email && (
                        <span className={styles.email}>{hit.person.primary_email}</span>
                      )}
                    </span>
                    <span className={styles.resultMeta}>
                      <Badge tone={meta.tone} dot>
                        {meta.label} {hit.best_strength}
                      </Badge>
                      {brokers.length > 0 && (
                        <span className={styles.brokers}>
                          {brokers.slice(0, 2).map((id) => (
                            <span key={id} className={styles.brokerChip}>
                              <Icons.UserCheckIcon /> via {memberName(id)}
                            </span>
                          ))}
                          {brokers.length > 2 && (
                            <span className={styles.brokerMore}>+{brokers.length - 2}</span>
                          )}
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
          ) : (
            <EmptyState
              icon={<Icons.UsersIcon />}
              title="No one in your network matches yet"
              description={
                hasSources
                  ? "Try broader terms, or import more contacts so your team's graph is denser."
                  : "Connect a source on the right and import contacts to build your network graph."
              }
            />
          )}
        </section>

        {/* ---- sources sidebar ---- */}
        <aside aria-label="Your network sources">
          <Card>
            <CardHeader title="Your sources" subtitle="Private by default. Share to pool with your team." />
            {sources.error && !sources.data ? (
              <ErrorState
                title="Couldn't load sources"
                message={sources.error.detail}
                onRetry={sources.refetch}
              />
            ) : sources.loading && !sources.data ? (
              <div className={styles.sourceList}>
                {[0, 1].map((i) => (
                  <Skeleton key={i} width="100%" height={84} />
                ))}
              </div>
            ) : hasSources ? (
              <div className={styles.sourceList}>
                {srcRows.map((acc) => (
                  <div key={acc.id} className={styles.sourceRow}>
                    <div className={styles.sourceTop}>
                      <span className={styles.sourceId}>
                        <Icons.PlugIcon className={styles.sourceIcon} />
                        <span className={styles.sourceEmail} title={acc.display_email}>
                          {acc.display_email || acc.provider}
                        </span>
                      </span>
                      <Badge tone={acc.status === "connected" ? "success" : "neutral"} dot>
                        {acc.status}
                      </Badge>
                    </div>
                    <div className={styles.sourceActions}>
                      <span className={styles.switchRow}>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={acc.pooling_enabled}
                          aria-label={`Share ${acc.display_email || acc.provider} with the team`}
                          className={styles.switch}
                          onClick={() => togglePooling(acc)}
                        >
                          <span className={styles.switchKnob} />
                        </button>
                        {acc.pooling_enabled ? "Shared" : "Private"}
                      </span>
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={busyId === acc.id}
                        iconLeft={<Icons.PlusIcon />}
                        onClick={() => importSample(acc)}
                      >
                        Import sample
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                compact
                icon={<Icons.PlugIcon />}
                title="No sources yet"
                description="Connect a source to start building your network graph."
              />
            )}

            <div className={styles.connectRow} style={{ marginTop: "var(--space-4)" }}>
              {PROVIDERS.map((p) => (
                <Button
                  key={p.id}
                  size="sm"
                  variant={p.id === "fixture" ? "primary" : "ghost"}
                  loading={connecting === p.id}
                  onClick={() => connect(p.id, p.label)}
                >
                  {p.label}
                </Button>
              ))}
            </div>
            <p className={styles.asideHint}>
              Google, Microsoft and LinkedIn connect instantly here; live sync over OAuth ships next.
              Use Demo network to populate the graph now.
            </p>
          </Card>
        </aside>
      </div>

      {/* ---- intro-path drawer ---- */}
      <Modal
        open={!!activePerson}
        onClose={() => setActivePerson(null)}
        title={activePerson?.full_name ?? "Intro paths"}
        description={
          activePerson
            ? [activePerson.title, activePerson.company].filter(Boolean).join(" · ") || undefined
            : undefined
        }
      >
        {pathsLoading ? (
          <div className={styles.inlineLoading}>
            <Spinner size={16} /> Finding the warmest paths…
          </div>
        ) : paths && paths.length > 0 ? (
          <>
            <p className={styles.drawerLead}>
              {paths.length === 1
                ? "One teammate can broker this introduction."
                : `${paths.length} teammates can broker this introduction, strongest first.`}
            </p>
            <ul className={styles.pathList}>
              {paths.map((p, i) => {
                const meta = strengthMeta(p.strength / 100);
                const how = RELATION_LABEL[p.relation] ?? p.relation;
                return (
                  <li key={`${p.broker_member_id}-${i}`} className={styles.pathRow}>
                    <span className={styles.pathBroker}>
                      <span className={styles.pathName}>{memberName(p.broker_member_id)}</span>
                      <span className={styles.pathMeta}>{how}</span>
                    </span>
                    <Badge tone={meta.tone} dot>
                      {meta.label} {p.strength}
                    </Badge>
                  </li>
                );
              })}
            </ul>
          </>
        ) : (
          <EmptyState
            compact
            icon={<Icons.UsersIcon />}
            title="No visible path yet"
            description="No one on your team has a shared relationship with this person."
          />
        )}
      </Modal>
    </div>
  );
}

export default NetworkPage;
