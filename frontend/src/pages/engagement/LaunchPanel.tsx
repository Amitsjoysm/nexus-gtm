import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, ErrorState, Icons, Skeleton } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { CreditEstimate } from "@/components/engagement/CreditEstimate";
import { VolumeWarning } from "@/components/engagement/VolumeWarning";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { CampaignEstimate, EngagementCampaign, ReviewItem } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Launch, or resume, a campaign behind the credit gate (spec §9 step 5, §10, D18).
 *
 * The button is held while the balance cannot cover the most the campaign can cost; the server
 * refuses the same way, so this only saves a round trip. Launching with nobody approved yet is
 * allowed and said plainly: the campaign starts, and each person goes out as they are approved.
 */

export interface LaunchPanelProps {
  campaign: EngagementCampaign;
  onLaunched: () => void;
}

export function LaunchPanel({ campaign, onLaunched }: LaunchPanelProps) {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const [launching, setLaunching] = useState(false);
  const estimate = useApi<CampaignEstimate>((s) => api.campaignEstimate(campaign.id, s), [campaign.id]);
  // Approved-and-waiting is a MESSAGE state: before launch every person is still awaiting review,
  // approved or not, so the count comes from the review queue rather than the enrollment counts.
  const review = useApi<ReviewItem[]>((s) => api.campaignReview(campaign.id, s), [campaign.id]);
  const approved = (review.data ?? []).filter((i) => i.status === "approved").length;
  const resuming = campaign.status === "paused";
  const canTopUp = session?.role === "admin" || session?.role === "owner";

  async function launch() {
    setLaunching(true);
    try {
      if (resuming) await api.campaignAction(campaign.id, "resume");
      else await api.launchCampaign(campaign.id);
      toast.success(resuming ? "Campaign resumed" : "Campaign launched",
        approved ? `${approved} ${approved === 1 ? "email goes" : "emails go"} out on schedule.` : "Each person goes out as you approve them.");
      onLaunched();
    } catch (err) {
      toast.error(resuming ? "Not resumed" : "Not launched", err instanceof ApiError ? err.detail : "Please try again.");
      estimate.refetch();
    } finally {
      setLaunching(false);
    }
  }

  if (estimate.error) {
    return <ErrorState title="Couldn't work out the cost" message={estimate.error.detail} onRetry={estimate.refetch} />;
  }
  if (!estimate.data) {
    return <Skeleton width="100%" height={260} />;
  }
  const e = estimate.data;
  const blocked = e.gate_applies && !e.covered;

  return (
    <div className={styles.stack}>
      <CreditEstimate estimate={e} />
      <VolumeWarning message={e.volume_warning} />
      {e.contacts === 0 ? (
        <p className={styles.notice} role="status">Add people before launching.</p>
      ) : approved === 0 ? (
        <p className={styles.notice} role="status">
          Nobody is approved yet. Launching starts the campaign, and each person goes out as you
          approve their email.
        </p>
      ) : null}
      <div className={styles.formActions}>
        {blocked && canTopUp && (
          <Link to="/settings/billing" className={styles.buttonLink}>Add credits</Link>
        )}
        <Button onClick={launch} loading={launching} disabled={blocked || e.contacts === 0}
          iconLeft={<Icons.SendIcon />}>
          {resuming ? "Resume campaign" : "Launch campaign"}
        </Button>
      </div>
      {blocked && !canTopUp && (
        <p className={styles.muted}>Ask an owner or admin to add credits to the workspace.</p>
      )}
    </div>
  );
}
