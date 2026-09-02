import { Card, EmptyState, Icons } from "@/components/ui";
import { PageHeader } from "@/components/layout/PageHeader";
import type { FeatureSwitchState } from "@/lib/types";
import styles from "./FeatureUnavailable.module.css";

/**
 * The screen a customer gets when a platform switch has taken a feature offline.
 *
 * NOT a redirect to billing. `RequireCapability` sends plan-gated routes there, which is right when
 * the customer could buy their way in — but a platform switch is our decision, and answering "we
 * turned this off" with "upgrade your plan" invites them to pay to fix our maintenance window.
 * They also cannot: no plan re-enables a switched-off module.
 *
 * The three states differ ONLY in what is said, which is the entire reason there are three of them
 * rather than one boolean. "We turned this off", "this is not built yet" and "this is broken right
 * now" lead to three different support conversations, and a rep repeating the wrong one to a
 * prospect is worse than saying nothing.
 */

const COPY: Record<
  Exclude<FeatureSwitchState, "enabled">,
  { title: string; body: string; icon: JSX.Element }
> = {
  coming_soon: {
    title: "Coming soon",
    // No date promised. A date we control is a commitment; a date in a banner nobody owns is a
    // complaint waiting to happen.
    body: "This is on the way. Nothing to set up now, and it will appear here when it ships.",
    icon: <Icons.SparklesIcon />,
  },
  maintenance: {
    title: "Temporarily unavailable",
    body: "We have taken this offline briefly to work on it. Your data is untouched and everything else keeps running.",
    icon: <Icons.RefreshIcon />,
  },
  disabled: {
    title: "Not available",
    body: "This has been switched off for all workspaces. Contact support if you were relying on it.",
    icon: <Icons.ShieldCheckIcon />,
  },
};

export interface FeatureUnavailableProps {
  /** Page title, so the shell still says where the user is rather than going blank. */
  name: string;
  state: Exclude<FeatureSwitchState, "enabled">;
  /** The operator's own wording. Replaces the generic sentence when set. */
  message?: string;
}

export function FeatureUnavailable({ name, state, message }: FeatureUnavailableProps) {
  const copy = COPY[state] ?? COPY.disabled;
  return (
    <div>
      {/* The heading still names the page. Someone who followed a bookmark or a colleague's link
          needs to know they arrived where they meant to, not that the route vanished. */}
      <PageHeader title={name} />
      <Card padding="lg">
        <EmptyState
          icon={copy.icon}
          title={copy.title}
          description={
            <>
              {/* Operator wording first when present: "back at 14:00 UTC" is the sentence a rep
                  can repeat, and the generic line is the fallback for a switch flipped in a hurry
                  with the message left blank. */}
              <span className={styles.body}>{message?.trim() || copy.body}</span>
              {message?.trim() && <span className={styles.sub}>{copy.body}</span>}
            </>
          }
        />
      </Card>
    </div>
  );
}

export default FeatureUnavailable;
