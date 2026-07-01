import { useEffect, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, Card, CardHeader, EmptyState, ErrorState, Icons, Input, Modal, Skeleton,
  Spinner, useToast,
} from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { strengthMeta } from "@/lib/display";
import type {
  Member, NetworkAccount, NetworkIntroPath, NetworkPersonSummary, NetworkSearchHit,
} from "@/lib/types";
import styles from "./NetworkPage.module.css";

const EXAMPLE_QUERIES = [
  "CTO at a healthcare startup",
  "VP of Procurement in retail",
  "CFO at a fintech",
  "Head of Talent",
];
const RELATION_LABEL: Record<string, string> = {
  email: "email thread", calendar: "met in person", linkedin_1st: "1st-degree connection",
  follower: "follows them", contact: "in contacts",
};

export function NetworkPage() {
  const api = useApiClient();
  const toast = useToast();

  const sources = useApi<NetworkAccount[]>((s) => api.listNetworkAccounts(s), []);
  const members = useApi<Member[]>((s) => api.listMembers(s), []);
  const memberName = (id: string) =>
    members.data?.find((m) => m.membership_id === id)?.full_name ?? "a teammate";

  // OAuth callback bounce: /network?connected=google or ?error=...
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const connected = p.get("connected");
    const error = p.get("error");
    if (connected) {
      toast.success("Account connected", `Syncing your ${connected} network now.`);
      sources.refetch();
    } else if (error) {
      toast.error("Couldn't connect", `OAuth failed (${error}). Please try again.`);
    }
    if (connected || error) {
      window.history.replaceState({}, "", "/network");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // search
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [results, setResults] = useState<NetworkSearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  async function runSearch(raw: string) {
    const term = raw.trim();
    if (!term) return;
    setQuery(term); setSubmitted(term); setSearching(true); setSearchError(null);
    try {
      setResults(await api.searchNetwork(term, 25));
    } catch (err) {
      setResults(null);
      setSearchError(err instanceof ApiError ? err.detail : "Search failed. Try again.");
    } finally {
      setSearching(false);
    }
  }

  // connect / sync
  const [connecting, setConnecting] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function connectOAuth(provider: "google" | "microsoft") {
    setConnecting(provider);
    try {
      const { authorize_url } = await api.networkOAuthStart(provider);
      window.location.assign(authorize_url); // leave the SPA for the consent screen
    } catch (err) {
      setConnecting(null);
      const msg = err instanceof ApiError ? err.detail : "Try again.";
      toast.error(`Can't connect ${provider}`, msg);
    }
  }

  const [linkedInAccountId, setLinkedInAccountId] = useState<string | null>(null);
  async function ensureLinkedInSource(): Promise<string> {
    const existing = (sources.data ?? []).find((a) => a.provider === "linkedin");
    if (existing) return existing.id;
    const acc = await api.connectNetworkAccount({
      provider: "linkedin", external_account_id: "linkedin-export",
      display_email: "LinkedIn (export)",
    });
    sources.refetch();
    return acc.id;
  }
  async function onLinkedInFile(file: File) {
    setConnecting("linkedin");
    try {
      const id = linkedInAccountId ?? (await ensureLinkedInSource());
      const res = await api.importLinkedInCsv(id, file);
      toast.success("LinkedIn import complete", `Added ${res.new_persons} connections.`);
      sources.refetch();
      if (submitted) runSearch(submitted);
    } catch (err) {
      toast.error("Import failed", err instanceof ApiError ? err.detail : "Check the CSV and retry.");
    } finally {
      setConnecting(null);
    }
  }

  async function togglePooling(acc: NetworkAccount) {
    const next = !acc.pooling_enabled;
    sources.setData((prev) => (prev ?? []).map((a) => (a.id === acc.id ? { ...a, pooling_enabled: next } : a)));
    try {
      await api.patchNetworkAccount(acc.id, { pooling_enabled: next });
    } catch (err) {
      sources.setData((prev) => (prev ?? []).map((a) => (a.id === acc.id ? { ...a, pooling_enabled: !next } : a)));
      toast.error("Couldn't update sharing", err instanceof ApiError ? err.detail : "Try again.");
    }
  }

  async function sync(acc: NetworkAccount) {
    setBusyId(acc.id);
    try {
      await api.syncNetworkAccount(acc.id);
      toast.success("Sync started", "We'll refresh this network in the background.");
    } catch (err) {
      toast.error("Couldn't start sync", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  // intro drawer
  const [activePerson, setActivePerson] = useState<NetworkPersonSummary | null>(null);
  const [paths, setPaths] = useState<NetworkIntroPath[] | null>(null);
  const [pathsLoading, setPathsLoading] = useState(false);
  async function openPerson(person: NetworkPersonSummary) {
    setActivePerson(person); setPaths(null); setPathsLoading(true);
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
  void linkedInAccountId; void setLinkedInAccountId;

  return (
    <div>
      <PageHeader
        title="Network"
        description="Search the people your team already knows, and find the warmest path to any buyer."
      />

      <div className={styles.searchCard}>
        <form className={styles.searchForm} onSubmit={(e) => { e.preventDefault(); runSearch(query); }}>
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Who do we know who's a CTO at a healthcare startup?"
            iconLeft={<Icons.SearchIcon />}
            aria-label="Search your network in plain language"
          />
          <Button type="submit" loading={searching} iconLeft={<Icons.SparklesIcon />}>Search</Button>
        </form>
        <div className={styles.examples}>
          <span className={styles.examplesLabel}>Try:</span>
          {EXAMPLE_QUERIES.map((q) => (
            <button key={q} type="button" className={styles.chip} onClick={() => runSearch(q)}>{q}</button>
          ))}
        </div>
      </div>

      <div className={styles.layout}>
        <section aria-label="Search results">
          <h2 className={styles.sectionTitle}>{submitted ? `Results for "${submitted}"` : "Results"}</h2>
          {searching ? (
            <div className={styles.results}>{[0, 1, 2].map((i) => <Skeleton key={i} width="100%" height={64} />)}</div>
          ) : searchError ? (
            <ErrorState title="Search failed" message={searchError} onRetry={() => submitted && runSearch(submitted)} />
          ) : !submitted ? (
            <EmptyState icon={<Icons.SearchIcon />} title="Ask in plain language"
              description="Describe who you're trying to reach. We rank the people your team already knows by match and relationship strength." />
          ) : results && results.length > 0 ? (
            <div className={styles.results}>
              {results.map((hit, i) => {
                const meta = strengthMeta(hit.best_strength / 100);
                const sub = [hit.person.title, hit.person.company].filter(Boolean).join(" · ");
                return (
                  <button key={hit.person.id} type="button"
                    className={`${styles.resultRow} ${styles.rise}`}
                    style={{ animationDelay: `${Math.min(i, 8) * 28}ms` }}
                    onClick={() => openPerson(hit.person)}
                    aria-label={`See intro paths to ${hit.person.full_name}`}>
                    <span className={styles.resultMain}>
                      <span className={styles.name}>{hit.person.full_name}</span>
                      {sub && <span className={styles.sub}>{sub}</span>}
                      {hit.person.primary_email && <span className={styles.email}>{hit.person.primary_email}</span>}
                    </span>
                    <span className={styles.resultMeta}>
                      <Badge tone={meta.tone} dot>{meta.label} {hit.best_strength}</Badge>
                      {hit.broker_member_ids.length > 0 && (
                        <span className={styles.brokers}>
                          {hit.broker_member_ids.slice(0, 2).map((id) => (
                            <span key={id} className={styles.brokerChip}><Icons.UserCheckIcon /> via {memberName(id)}</span>
                          ))}
                          {hit.broker_member_ids.length > 2 && (
                            <span className={styles.brokerMore}>+{hit.broker_member_ids.length - 2}</span>
                          )}
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
          ) : (
            <EmptyState icon={<Icons.UsersIcon />} title="No one in your network matches yet"
              description={hasSources
                ? "Try broader terms, or connect another source so your team's graph is denser."
                : "Connect a source on the right to build your network graph."} />
          )}
        </section>

        <aside aria-label="Your network sources">
          <Card>
            <CardHeader title="Your sources" subtitle="Private by default. Share to pool with your team." />
            {sources.error && !sources.data ? (
              <ErrorState title="Couldn't load sources" message={sources.error.detail} onRetry={sources.refetch} />
            ) : sources.loading && !sources.data ? (
              <div className={styles.sourceList}>{[0, 1].map((i) => <Skeleton key={i} width="100%" height={84} />)}</div>
            ) : hasSources ? (
              <div className={styles.sourceList}>
                {srcRows.map((acc) => (
                  <div key={acc.id} className={styles.sourceRow}>
                    <div className={styles.sourceTop}>
                      <span className={styles.sourceId}>
                        <Icons.PlugIcon className={styles.sourceIcon} />
                        <span className={styles.sourceEmail} title={acc.display_email}>{acc.display_email || acc.provider}</span>
                      </span>
                      <Badge tone={acc.status === "connected" ? "success" : acc.status === "error" ? "danger" : "neutral"} dot>{acc.status}</Badge>
                    </div>
                    <div className={styles.sourceActions}>
                      <span className={styles.switchRow}>
                        <button type="button" role="switch" aria-checked={acc.pooling_enabled}
                          aria-label={`Share ${acc.display_email || acc.provider} with the team`}
                          className={styles.switch} onClick={() => togglePooling(acc)}>
                          <span className={styles.switchKnob} />
                        </button>
                        {acc.pooling_enabled ? "Shared" : "Private"}
                      </span>
                      {acc.status === "error" && acc.provider !== "linkedin" ? (
                        <Button size="sm" variant="secondary" iconLeft={<Icons.RefreshIcon />}
                          onClick={() => connectOAuth(acc.provider as "google" | "microsoft")}>Reconnect</Button>
                      ) : acc.provider !== "linkedin" ? (
                        <Button size="sm" variant="ghost" loading={busyId === acc.id}
                          iconLeft={<Icons.RefreshIcon />} onClick={() => sync(acc)}>Sync</Button>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState compact icon={<Icons.PlugIcon />} title="No sources yet"
                description="Connect Google, Microsoft, or upload your LinkedIn export to begin." />
            )}

            <div className={styles.connectRow} style={{ marginTop: "var(--space-4)" }}>
              <Button size="sm" variant="primary" loading={connecting === "google"} onClick={() => connectOAuth("google")}>Connect Google</Button>
              <Button size="sm" variant="ghost" loading={connecting === "microsoft"} onClick={() => connectOAuth("microsoft")}>Connect Microsoft</Button>
              <label className={styles.uploadBtn}>
                <input type="file" accept=".csv" hidden
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) onLinkedInFile(f); e.currentTarget.value = ""; }} />
                {connecting === "linkedin" ? "Importing…" : "Upload LinkedIn CSV"}
              </label>
            </div>
            <p className={styles.asideHint}>
              Google/Microsoft sync your contacts and meetings over OAuth. For LinkedIn, upload your
              own export (Settings → Get a copy of your data → Connections).
            </p>
          </Card>
        </aside>
      </div>

      <Modal open={!!activePerson} onClose={() => setActivePerson(null)}
        title={activePerson?.full_name ?? "Intro paths"}
        description={activePerson ? [activePerson.title, activePerson.company].filter(Boolean).join(" · ") || undefined : undefined}>
        {pathsLoading ? (
          <div className={styles.inlineLoading}><Spinner size={16} /> Finding the warmest paths…</div>
        ) : paths && paths.length > 0 ? (
          <>
            <p className={styles.drawerLead}>
              {paths.length === 1 ? "One teammate can broker this introduction."
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
                    <Badge tone={meta.tone} dot>{meta.label} {p.strength}</Badge>
                  </li>
                );
              })}
            </ul>
          </>
        ) : (
          <EmptyState compact icon={<Icons.UsersIcon />} title="No visible path yet"
            description="No one on your team has a shared relationship with this person." />
        )}
      </Modal>
    </div>
  );
}

export default NetworkPage;
