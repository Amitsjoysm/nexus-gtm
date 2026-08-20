import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/app/AuthContext";
import { NAV_ITEMS } from "@/app/nav";
import { EntitlementsProvider } from "@/app/EntitlementsContext";
import { ImpersonationBanner } from "./ImpersonationBanner";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import styles from "./AppShell.module.css";

export const APP_NAME = "InfoJoy GTM";

/**
 * Route path -> page title, derived from the navigation rather than hand-maintained.
 *
 * This used to be a literal map with seven entries for twenty-two destinations, so Calls,
 * Contacts, Network, Lists, AI Runs, Approvals, Campaigns, Cadences, Plays, Relevance,
 * Integrations, Settings and both admin pages all fell through to the app name and rendered as
 * "InfoJoy GTM · InfoJoy GTM". A second list of every page was always going to drift from the
 * first; `NAV_ITEMS` already names each one, so deriving from it means adding a page cannot
 * forget to add its title.
 *
 * Sorted longest-first so `/settings/billing` matches "Billing" rather than "Settings" — a plain
 * first-segment lookup, which is what the old code did, cannot distinguish them.
 */
const TITLE_ROUTES: ReadonlyArray<readonly [string, string]> = NAV_ITEMS.map(
  (item) => [item.to, item.label] as const,
).sort((a, b) => b[0].length - a[0].length);

export function titleFor(pathname: string): string {
  const path = pathname.replace(/\/+$/, "") || "/dashboard";
  for (const [to, label] of TITLE_ROUTES) {
    if (path === to || path.startsWith(`${to}/`)) return label;
  }
  return APP_NAME;
}

/** Authenticated app layout: sidebar + topbar + routed content, responsive drawer. */
export function AppShell() {
  const location = useLocation();
  const { tenantEpoch } = useAuth();
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Desktop sidebar collapse (icons-only), remembered across sessions.
  const [collapsed, setCollapsed] = useState(
    () => typeof localStorage !== "undefined" && localStorage.getItem("sidebar-collapsed") === "1",
  );
  const title = titleFor(location.pathname);

  // Close the mobile drawer on route change; keep the document title in sync.
  useEffect(() => {
    setDrawerOpen(false);
    // A page whose title IS the app name must not render as "InfoJoy GTM · InfoJoy GTM".
    document.title = title === APP_NAME ? APP_NAME : `${title} · ${APP_NAME}`;
  }, [location.pathname, title]);

  function toggleCollapse() {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("sidebar-collapsed", next ? "1" : "0");
      } catch {
        /* storage may be unavailable (private mode) — collapse still works for the session */
      }
      return next;
    });
  }

  return (
    // Wraps the whole shell, not just the sidebar: page-level empty states and headers ask the
    // same "is this in our plan?" question, and one fetch answers all of them.
    <EntitlementsProvider>
      <ImpersonationBanner />
      <div className={styles.shell}>
        <a className="skip-link" href="#main">
          Skip to content
        </a>

        <Sidebar
          open={drawerOpen}
          collapsed={collapsed}
          onNavigate={() => setDrawerOpen(false)}
          onToggleCollapse={toggleCollapse}
        />

        {drawerOpen && (
          <button
            className={styles.scrim}
            aria-label="Close menu"
            onClick={() => setDrawerOpen(false)}
          />
        )}

        <div className={styles.body}>
          <Topbar title={title} onMenuClick={() => setDrawerOpen(true)} />
          <main id="main" className={styles.main} tabIndex={-1}>
            <div className={styles.container} key={tenantEpoch}>
              <Outlet />
            </div>
          </main>
        </div>
      </div>
    </EntitlementsProvider>
  );
}
