import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Icons } from "@/components/ui";
import { ChannelRules } from "@/components/alerts/ChannelRules";
import { useCanConnectChannels } from "@/components/alerts/useCanConnect";
import { AlertDelivery } from "./settings/AlertDelivery";
import styles from "./AlertSettingsPage.module.css";

/**
 * Where alerts go, on a page every member can open.
 *
 * Alert delivery used to sit on Settings, which is admin-only in the nav and in the route guard, so
 * the rep-level routes API had a screen no rep or manager could reach. It lives beside Alerts now,
 * gated the way Alerts is rather than the way workspace administration is.
 *
 * Two halves, in the order people need them: your own routes first (every member), then what the
 * team's shared channels receive (managers and up, the people who connect those channels).
 */
export function AlertSettingsPage() {
  const canManageChannels = useCanConnectChannels();
  return (
    <div>
      <PageHeader
        eyebrow={
          <Link to="/alerts" className={styles.back}>
            <Icons.ChevronLeftIcon /> Alerts
          </Link>
        }
        title="Alert settings"
        description="Choose where alerts reach you. Managers also choose which alert types the team's shared channels receive."
      />
      <div className={styles.stack}>
        <AlertDelivery />
        {canManageChannels && <ChannelRules />}
      </div>
    </div>
  );
}

export default AlertSettingsPage;
