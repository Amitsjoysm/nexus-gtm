import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge, Button, DataTable, EmptyState, ErrorState, Icons } from "@/components/ui";
import type { Column } from "@/components/ui";
import { CAMPAIGN_STATUS, reasonText, whenDay } from "@/components/engagement/labels";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useEngagementStatus } from "@/app/EngagementContext";
import type { ConnectedMailbox, EngagementCampaign } from "@/lib/types";
import styles from "./Engagement.module.css";

/**
 * Campaigns on the engagement engine (spec §9): sequences an SDR sends from their own mailbox.
 *
 * Every member builds their own; a manager can look across the team. The list is the way into a
 * campaign, and the campaign page is where it is built, reviewed, launched and steered.
 */

function total(counts: Record<string, number>): number {
  return Object.values(counts).reduce((a, b) => a + b, 0);
}

export function EngagementCampaignsPage() {
  const api = useApiClient();
  const navigate = useNavigate();
  const status = useEngagementStatus();
  const [team, setTeam] = useState(false);
  const campaigns = useApi<EngagementCampaign[]>((s) => api.listEngagementCampaigns(team, s), [team]);
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(false, s), []);
  const hasMailbox = (mailboxes.data ?? []).some((m) => m.mine && m.status === "connected");

  const columns: Column<EngagementCampaign>[] = [
    {
      key: "name",
      header: "Campaign",
      sortable: true,
      sortValue: (c) => c.name.toLowerCase(),
      render: (c) => (
        <Link to={`/engagement/campaigns/${c.id}`} className={styles.rowLink}>{c.name}</Link>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (c) => {
        const s = CAMPAIGN_STATUS[c.status] ?? { label: c.status, tone: "neutral" as const };
        return (
          <span className={styles.statusCell}>
            <Badge tone={s.tone} dot>{s.label}</Badge>
            {c.status === "paused" && c.pause_reason && c.pause_reason !== "manual" && (
              <span className={styles.muted}>{reasonText(c.pause_reason)}</span>
            )}
          </span>
        );
      },
    },
    {
      key: "people",
      header: "People",
      align: "right",
      sortable: true,
      sortValue: (c) => total(c.counts),
      render: (c) => <span className={styles.num}>{total(c.counts)}</span>,
    },
    {
      key: "active",
      header: "In sequence",
      align: "right",
      hideOnMobile: true,
      render: (c) => <span className={styles.num}>{c.counts.active ?? 0}</span>,
    },
    {
      key: "steps",
      header: "Steps",
      align: "right",
      hideOnMobile: true,
      render: (c) => <span className={styles.num}>{c.steps.length}</span>,
    },
    {
      key: "launched",
      header: "Launched",
      hideOnMobile: true,
      sortable: true,
      sortValue: (c) => c.launched_at ?? "",
      render: (c) => (c.launched_at ? whenDay(c.launched_at) : <span className={styles.muted}>Not yet</span>),
    },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Campaigns"
        description="Sequences sent from your own mailbox. Every first email waits for your approval, and replies arrive in Replies."
        actions={
          <>
            {status?.can_manage && (
              <Button variant="secondary" size="sm" aria-pressed={team} onClick={() => setTeam((t) => !t)}>
                {team ? "Show mine" : "Show team"}
              </Button>
            )}
            <Button
              iconLeft={<Icons.PlusIcon />}
              onClick={() => navigate("/engagement/campaigns/new")}
              disabled={mailboxes.data !== null && !hasMailbox}
            >
              New campaign
            </Button>
          </>
        }
      />

      {mailboxes.data !== null && !hasMailbox && (
        <p className={styles.notice} role="status">
          Campaigns send from your own Gmail or Outlook mailbox.{" "}
          <Link to="/mailboxes" className={styles.inlineLink}>Connect it on My mailboxes</Link>{" "}
          to build one.
        </p>
      )}

      {campaigns.error ? (
        <ErrorState title="Couldn't load campaigns" message={campaigns.error.detail} onRetry={campaigns.refetch} />
      ) : (
        <DataTable
          columns={columns}
          rows={campaigns.data ?? []}
          getRowKey={(c) => c.id}
          loading={campaigns.loading && !campaigns.data}
          onRowClick={(c) => navigate(`/engagement/campaigns/${c.id}`)}
          caption="Campaigns"
          empty={
            <EmptyState
              icon={<Icons.SendIcon />}
              title={team ? "Nobody on the team has a campaign yet" : "No campaigns yet"}
              description="Pick the people, choose the steps, read every first email, then launch. Nothing sends until you approve it."
              action={hasMailbox ? (
                <Button iconLeft={<Icons.PlusIcon />} onClick={() => navigate("/engagement/campaigns/new")}>
                  New campaign
                </Button>
              ) : undefined}
            />
          }
        />
      )}
    </div>
  );
}

export default EngagementCampaignsPage;
