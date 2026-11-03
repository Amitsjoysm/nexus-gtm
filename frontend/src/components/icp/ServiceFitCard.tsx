import { Button, Card, CardHeader, ScoreMeter, Skeleton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import type { ServiceFit } from "@/lib/types";
import { FitBadge } from "./FitBadge";
import styles from "./ServiceFitCard.module.css";

/**
 * Account 360: how well this company fits each service the workspace sells (W8, owner decision
 * 2026-10-29). Shown only once there is more than one service; with one, the ICP fit fact above
 * already says it all. Each row is the ICP fit against that service's own ICP, and a service whose
 * must-match fields the company misses says which, never a percentage.
 */
export function ServiceFitCard({ accountId }: { accountId: string }) {
  const api = useApiClient();
  const fits = useApi<ServiceFit[]>((signal) => api.accountServiceFits(accountId, signal), [
    accountId,
  ]);

  if (fits.data && fits.data.length < 2) return null;
  if (!fits.data && !fits.loading && !fits.error) return null;

  return (
    <Card padding="lg" className={styles.card} aria-labelledby="service-fit-title">
      <CardHeader
        title={<span id="service-fit-title">Fit by service</span>}
        subtitle="How well this company matches the ICP of each service you sell. Find contacts and its call scripts use the service that found it."
      />
      {fits.error ? (
        <p className={styles.error} role="alert">
          Couldn&apos;t load the fit for each service.{" "}
          <Button size="sm" variant="ghost" onClick={() => fits.refetch()}>
            Retry
          </Button>
        </p>
      ) : !fits.data ? (
        <ul className={styles.list} aria-busy="true">
          {[0, 1].map((i) => (
            <li key={i} className={styles.row}>
              <Skeleton width="40%" height={14} />
              <Skeleton width="100%" height={8} />
            </li>
          ))}
        </ul>
      ) : (
        <ul className={styles.list}>
          {fits.data.map((fit) => {
            const missed = fit.gate && !fit.gate.passed;
            return (
              <li key={fit.service_id ?? "main"} className={styles.row}>
                <div className={styles.head}>
                  <span className={styles.name}>{fit.name}</span>
                  {fit.found_here && <span className={styles.found}>Found it</span>}
                </div>
                <div className={styles.score}>
                  {fit.icp_fit == null ? (
                    <span className={styles.muted}>No ICP saved yet</span>
                  ) : missed ? (
                    <FitBadge gate={fit.gate} />
                  ) : (
                    <>
                      <ScoreMeter value={fit.icp_fit} className={styles.meter} />
                      <FitBadge score={fit.icp_fit} />
                    </>
                  )}
                </div>
                {fit.icp_fit != null && (missed || fit.reasons.length > 0) && (
                  <p className={styles.reason}>
                    {missed ? fit.gate!.reason : fit.reasons.slice(0, 2).join("; ")}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
