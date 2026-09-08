import { useState } from "react";
import { Badge, Button, CardHeader, Icons, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { SharedCrawlCompany, SharedCrawlSummary } from "@/lib/types";
import styles from "./SharedCrawlTab.module.css";

/**
 * Approve, per company, that the shared crawl may deliver.
 *
 * `nexus/companies/` is a four-stage rollout: backfill, shadow crawl, **diff**, fan-out. Stages 1,
 * 2 and 4 shipped; stage 3 shipped as a library with no endpoint and no caller outside tests. Both
 * gates read `companies.crawl_verdict`, nothing in production wrote it, so every company sat at
 * `unknown`: the shared crawl gathered and delivered nothing while the per-tenant crawl still ran
 * in full. Measured on the live deployment, 106 companies crawled and 0 delivered.
 *
 * **A person decides, and that is the design.** The subsystem's own rule: do not enable fan-out on
 * assertion, because it multiplies any attribution mistake by the number of subscribing tenants,
 * and four of its six attribution bugs were found only by running against live providers. A job
 * that promoted a company because two crawls happened to match is promotion on assertion wearing a
 * schedule. So this screen shows the evidence and records the conclusion.
 *
 * Read the evidence asymmetrically. Signals only the SHARED crawl has are usually fine — it ran
 * more recently. Signals only a TENANT has are the failure: fan-out would then deliver less than
 * that tenant already sees, which reads as data loss.
 */
export function SharedCrawlTab() {
  const api = useApiClient();
  const toast = useToast();
  const [onlyUnproven, setOnlyUnproven] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const summary = useApi<SharedCrawlSummary>((signal) => api.sharedCrawlSummary(signal), []);
  const companies = useApi<SharedCrawlCompany[]>(
    (signal) => api.sharedCrawlCompanies({ limit: 25, only_unproven: onlyUnproven }, signal),
    [onlyUnproven],
  );

  async function decide(company: SharedCrawlCompany, agrees: boolean) {
    setBusy(company.company_id);
    try {
      await api.setSharedCrawlVerdict(company.company_id, agrees);
      toast.success(
        agrees ? `${company.name} approved` : `${company.name} marked as disagreeing`,
        agrees
          ? "Its signals now reach every workspace tracking it, and the duplicate per-tenant crawl stops."
          : "Recorded as a finding. The per-tenant crawl keeps running for it.",
      );
      companies.refetch();
      summary.refetch();
    } catch (err) {
      toast.error(
        "Couldn't record that",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className={styles.wrap}>
      <CardHeader
        title="Shared crawl approvals"
        subtitle="One crawl per real-world company, shared across every workspace that tracks it. A company delivers only once somebody has compared it against what tenants already see."
      />

      <DataState
        state={summary}
        errorTitle="Couldn't load the summary"
        skeleton={<Skeleton width="100%" height={90} />}
      >
        {(s) => {
          const unknown = s.companies.unknown ?? 0;
          const approved = s.companies.agrees ?? 0;
          return (
            <>
              <div className={styles.stats}>
                <Stat label="Approved" value={approved} tone="success" />
                <Stat label="Awaiting a verdict" value={unknown} tone={unknown ? "warning" : undefined} />
                <Stat label="Recorded as wrong" value={s.companies.disagrees ?? 0} />
                <Stat label="Accounts served" value={s.accounts_served_by_shared_crawl} />
              </div>
              {approved === 0 && (
                <p className={styles.callout}>
                  Nothing is approved, so the shared crawl is gathering signals that reach nobody
                  while every account is still crawled per workspace. Approving a company is what
                  turns the second crawl off for it.
                </p>
              )}
              {!s.fanout_enabled && (
                <p className={styles.callout}>
                  Fan-out is switched off for the whole deployment, so an approval here is recorded
                  and changes nothing until it is switched on.
                </p>
              )}
            </>
          );
        }}
      </DataState>

      <div className={styles.filter}>
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={onlyUnproven}
            onChange={(e) => setOnlyUnproven(e.target.checked)}
          />
          <span>Only companies awaiting a verdict</span>
        </label>
        <Button
          size="sm"
          variant="ghost"
          iconLeft={<Icons.RefreshIcon />}
          loading={companies.loading}
          onClick={() => {
            companies.refetch();
            summary.refetch();
          }}
        >
          Re-run the comparison
        </Button>
      </div>

      <DataState
        state={companies}
        errorTitle="Couldn't compare the crawls"
        skeleton={<Skeleton width="100%" height={220} />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <p className={styles.empty}>
            {onlyUnproven
              ? "Every company has a verdict. Uncheck the filter to review the ones already decided."
              : "No companies have been linked and crawled yet."}
          </p>
        }
      >
        {(rows) => (
          <ul className={styles.list}>
            {rows.map((c) => (
              <li key={c.company_id} className={styles.row}>
                <div className={styles.head}>
                  <div className={styles.headText}>
                    <span className={styles.name}>{c.name}</span>
                    <span className={styles.domain}>{c.domain}</span>
                  </div>
                  <VerdictBadge company={c} />
                </div>

                <p className={styles.evidence}>
                  {!c.comparable ? (
                    c.last_crawled_at === null ? (
                      "Never crawled, so there is nothing to compare yet."
                    ) : (
                      "Linked to no accounts with signals, so there is nothing to compare."
                    )
                  ) : (
                    <>
                      {formatNumber(c.accounts_agreeing)} of{" "}
                      {formatNumber(c.accounts_agreeing + c.accounts_disagreeing)} workspaces
                      tracking this company would see everything they see today.
                      {c.accounts_disagreeing > 0 && (
                        <>
                          {" "}
                          {formatNumber(c.accounts_disagreeing)} would lose signals the shared crawl
                          has not found.
                        </>
                      )}
                    </>
                  )}
                </p>

                {c.missing_from_shared.length > 0 && (
                  <details className={styles.missing}>
                    <summary>
                      {c.missing_from_shared.length} signal
                      {c.missing_from_shared.length === 1 ? "" : "s"} a workspace has and the shared
                      crawl does not
                    </summary>
                    <ul className={styles.keys}>
                      {c.missing_from_shared.map((k) => (
                        <li key={k}>
                          <code>{k}</code>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}

                <div className={styles.actions}>
                  <Button
                    size="sm"
                    disabled={!c.comparable || !c.would_agree || c.verdict === "agrees"}
                    loading={busy === c.company_id}
                    onClick={() => decide(c, true)}
                  >
                    Approve for delivery
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={c.verdict === "disagrees"}
                    loading={busy === c.company_id}
                    onClick={() => decide(c, false)}
                  >
                    Record as wrong
                  </Button>
                  {c.comparable && !c.would_agree && (
                    <span className={styles.blocked}>
                      Approval is blocked while a workspace would lose signals.
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </DataState>
    </div>
  );
}

function VerdictBadge({ company }: { company: SharedCrawlCompany }) {
  if (company.verdict === "agrees") {
    return (
      <Badge tone="success" dot>
        Delivering
      </Badge>
    );
  }
  if (company.verdict === "disagrees") {
    return (
      <Badge tone="danger" dot>
        Held back
      </Badge>
    );
  }
  return (
    <Badge tone="neutral" dot>
      Awaiting a verdict
    </Badge>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "success" | "warning";
}) {
  return (
    <div className={styles.stat}>
      <span className={styles.statLabel}>{label}</span>
      <span className={styles.statValue} data-tone={tone}>
        {formatNumber(value)}
      </span>
    </div>
  );
}

export default SharedCrawlTab;
