import { useEffect, useRef } from "react";
import { Navigate, NavLink, Outlet, useLocation } from "react-router-dom";
import { PageHeader, NestedPageContext } from "@/components/layout/PageHeader";
import { useAuth } from "@/app/AuthContext";
import { canSee, navState, type NavState } from "@/app/nav";
import { isLocked, isPageHidden, switchNotice, useEntitlements } from "@/app/EntitlementsContext";
import { useEngineOn } from "@/app/EngagementContext";
import { usePlatformIdentity } from "@/app/RequirePlatformAdmin";
import { SETTINGS_GROUPS, SETTINGS_TABS, tabPage, type SettingsTab } from "@/app/settingsTabs";
import { Icons } from "@/components/ui";
import { cn } from "@/lib/cn";
import styles from "./SettingsLayout.module.css";

interface VisibleTab {
  tab: SettingsTab;
  state: Exclude<NavState, "hidden">;
}

/** The tabs this person may see, by the same rules as the main menu. */
export function useSettingsTabs(): VisibleTab[] {
  const { session } = useAuth();
  const role = session?.role;
  const isPlatformAdmin = Boolean(usePlatformIdentity()?.is_platform_admin);
  const entitlements = useEntitlements();
  const engineOn = useEngineOn();
  const visible: VisibleTab[] = [];
  for (const tab of SETTINGS_TABS) {
    if (!canSee(tab, role, isPlatformAdmin, engineOn)) continue;
    if (isPageHidden(entitlements, tabPage(tab))) continue;
    const notice = switchNotice(entitlements, tab.capability);
    const state = navState(role, isLocked(entitlements, tab.capability), notice !== null);
    if (state !== "hidden") visible.push({ tab, state });
  }
  return visible;
}

/**
 * Settings: one frame, tabs grouped by whose setting it is (`app/settingsTabs.tsx`). Each tab is
 * its own route, so a tab can be linked to, bookmarked and refreshed, and the browser's Back button
 * moves between tabs the way it moves between pages.
 *
 * The menu is a `<nav>` of links with `aria-current`, not an ARIA tablist: every tab changes the
 * address, and a tablist promises arrow-key switching inside one page, which this is not.
 */
export function SettingsLayout() {
  const tabs = useSettingsTabs();
  const location = useLocation();
  const menu = useRef<HTMLElement>(null);

  // On the narrow strip, keep the current tab in view when arriving from a link.
  useEffect(() => {
    const active = menu.current?.querySelector<HTMLElement>("[aria-current=page]");
    active?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [location.pathname]);

  return (
    <div>
      <PageHeader
        title="Settings"
        description="Your mailboxes, AI apps and notifications, and how your team and workspace run."
      />
      <div className={styles.layout}>
        <nav ref={menu} className={styles.menu} aria-label="Settings">
          {SETTINGS_GROUPS.map((group) => {
            const mine = tabs.filter((t) => t.tab.group === group.id);
            if (!mine.length) return null;
            const labelId = `settings-group-${group.id}`;
            return (
              <div key={group.id} className={styles.group}>
                <h3 id={labelId} className={styles.groupLabel}>{group.label}</h3>
                <ul className={styles.list} aria-labelledby={labelId}>
                  {mine.map(({ tab, state }) => (
                    <li key={tab.to}>
                      <TabLink tab={tab} state={state} />
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </nav>
        <div className={styles.content}>
          <NestedPageContext.Provider value={true}>
            <Outlet />
          </NestedPageContext.Provider>
        </div>
      </div>
    </div>
  );
}

function TabLink({ tab, state }: VisibleTab) {
  const reasonId = `settings-tab-${tab.to.replace(/\W/g, "")}`;
  if (state === "locked") {
    // The same rule as the main menu: a module the plan lacks is an upsell for someone who can
    // buy it, so it links to Billing rather than to a page the server would refuse.
    return (
      <NavLink to="/settings/billing" className={styles.link} aria-describedby={reasonId}
        title={`${tab.label} is not included in your plan`}>
        <span className={styles.icon} aria-hidden="true">{tab.icon}</span>
        <span className={styles.linkLabel}>{tab.label}</span>
        <span className={styles.badge} aria-hidden="true"><Icons.LockIcon /></span>
        <span id={reasonId} className={styles.srOnly}>not included in your plan</span>
      </NavLink>
    );
  }
  return (
    <NavLink to={tab.to} className={({ isActive }) => cn(styles.link, isActive && styles.active)}
      aria-describedby={state === "unavailable" ? reasonId : undefined}>
      <span className={styles.icon} aria-hidden="true">{tab.icon}</span>
      <span className={styles.linkLabel}>{tab.label}</span>
      {state === "unavailable" && (
        <>
          <span className={styles.badge} aria-hidden="true">Off</span>
          <span id={reasonId} className={styles.srOnly}>switched off for now</span>
        </>
      )}
    </NavLink>
  );
}

/**
 * `/settings` itself: the workspace tab for the people who run the workspace (what Settings was
 * before it had tabs), and the first tab anyone else can open.
 */
export function SettingsIndex() {
  const tabs = useSettingsTabs();
  const { session } = useAuth();
  const runsWorkspace = session?.role === "admin" || session?.role === "owner";
  const open = tabs.filter((t) => t.state !== "locked");
  const preferred = runsWorkspace ? open.find((t) => t.tab.to === "/settings/workspace") : undefined;
  const target = preferred ?? open[0];
  return <Navigate to={target ? target.tab.to : "/dashboard"} replace />;
}

export default SettingsLayout;
