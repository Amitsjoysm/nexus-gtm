import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Icons, IconButton } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import type { AnalyticsOverview, RelevanceProfile } from "@/lib/types";
import styles from "./Onboarding.module.css";

interface Step {
  key: string;
  title: string;
  desc: string;
  done: boolean;
  cta: string;
  icon: ReactNode;
  onClick: () => void;
  loading?: boolean;
}

interface ActivationChecklistProps {
  /** The dashboard's already-fetched analytics overview (null while loading). */
  overview: AnalyticsOverview | null;
  /** Seeds a demo account + runs the pipeline (owned by the dashboard). */
  onSeed: () => void;
  seeding: boolean;
}

/**
 * First-run activation checklist. Derives each step's completion from live workspace
 * data (analytics overview + relevance profile) rather than client-side flags, so it
 * stays honest across devices and reloads. Hides itself once the workspace is activated
 * or the user dismisses it (persisted per tenant).
 */
export function ActivationChecklist({ overview, onSeed, seeding }: ActivationChecklistProps) {
  const api = useApiClient();
  const navigate = useNavigate();
  const { session } = useAuth();
  const relevance = useApi<RelevanceProfile>((signal) => api.getRelevanceProfile(signal), []);

  const dismissKey = session ? `nexus.onboarding.dismissed.${session.tenantId}` : "";
  const [dismissed, setDismissed] = useState(false);
  useEffect(() => {
    if (!dismissKey) {
      setDismissed(false);
      return;
    }
    try {
      setDismissed(localStorage.getItem(dismissKey) === "1");
    } catch {
      setDismissed(false);
    }
  }, [dismissKey]);

  function dismiss() {
    if (dismissKey) {
      try {
        localStorage.setItem(dismissKey, "1");
      } catch {
        /* private mode / storage disabled — dismiss for this view only */
      }
    }
    setDismissed(true);
  }

  // Wait until both data sources have settled so step states are correct on first paint.
  if (!overview || relevance.loading || dismissed) return null;

  const accounts = overview.accounts ?? 0;
  const avgScore = overview.avg_composite_score ?? 0;
  const plays = overview.plays_executed ?? 0;
  const icp = relevance.data?.icp;
  const icpTuned = Boolean(
    (icp?.industries?.length ?? 0) > 0 ||
      (icp?.countries?.length ?? 0) > 0 ||
      (relevance.data?.value_props?.length ?? 0) > 0,
  );

  const steps: Step[] = [
    {
      key: "accounts",
      title: "Add your first account",
      desc: "Seed a demo account or import your own to start scoring.",
      done: accounts > 0,
      cta: "Seed demo",
      icon: <Icons.SparklesIcon />,
      onClick: onSeed,
      loading: seeding,
    },
    {
      key: "score",
      title: "Enrich & score an account",
      desc: "Run enrichment to compute fit and surface buying signals.",
      done: avgScore > 0,
      cta: "View accounts",
      icon: <Icons.BuildingIcon />,
      onClick: () => navigate("/accounts"),
    },
    {
      key: "icp",
      title: "Tune your ICP",
      desc: "Tell NEXUS who you sell to so fit scores reflect your business.",
      done: icpTuned,
      cta: "Define ICP",
      icon: <Icons.TargetIcon />,
      onClick: () => navigate("/relevance"),
    },
    {
      key: "play",
      title: "Run your first play",
      desc: "Automate research and outreach across a segment of accounts.",
      done: plays > 0,
      cta: "Browse plays",
      icon: <Icons.BoltIcon />,
      onClick: () => navigate("/plays"),
    },
  ];

  const completed = steps.filter((s) => s.done).length;
  if (completed === steps.length) return null; // fully activated — stop nudging
  const activeIndex = steps.findIndex((s) => !s.done);

  return (
    <Card padding="md" className={styles.card}>
      <section aria-label="Setup checklist">
        <div className={styles.head}>
          <div>
            <h2 className={styles.heading}>Finish setting up NEXUS</h2>
            <p className={styles.sub}>Four steps to a working pipeline.</p>
          </div>
          <IconButton
            label="Dismiss setup checklist"
            icon={<Icons.XIcon />}
            onClick={dismiss}
          />
        </div>

        <div className={styles.progress}>
          <div
            className={styles.progressTrack}
            role="progressbar"
            aria-label="Setup progress"
            aria-valuemin={0}
            aria-valuemax={steps.length}
            aria-valuenow={completed}
          >
            <span
              className={styles.progressFill}
              style={{ width: `${(completed / steps.length) * 100}%` }}
            />
          </div>
          <span className={styles.progressLabel}>
            {completed} of {steps.length}
          </span>
        </div>

        <ol className={styles.steps}>
          {steps.map((step, i) => {
            const state = step.done ? "done" : i === activeIndex ? "active" : "pending";
            return (
              <li key={step.key} className={styles.step} data-state={state}>
                <span className={styles.node} aria-hidden="true">
                  {step.done ? <Icons.CheckIcon /> : i + 1}
                </span>
                <div className={styles.body}>
                  <div className={styles.stepTitle}>
                    {step.title}
                    <span className={styles.srOnly}>
                      {step.done ? " — completed" : " — to do"}
                    </span>
                  </div>
                  <p className={styles.stepDesc}>{step.desc}</p>
                </div>
                {step.done ? (
                  <span className={styles.doneTag} aria-hidden="true">
                    <Icons.CheckIcon /> Done
                  </span>
                ) : (
                  <Button
                    className={styles.action}
                    variant={state === "active" ? "secondary" : "ghost"}
                    size="sm"
                    iconLeft={step.icon}
                    loading={step.loading}
                    onClick={step.onClick}
                  >
                    {step.cta}
                  </Button>
                )}
              </li>
            );
          })}
        </ol>
      </section>
    </Card>
  );
}
