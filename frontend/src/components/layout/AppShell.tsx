import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/app/AuthContext";
import { EntitlementsProvider } from "@/app/EntitlementsContext";
import { ImpersonationBanner } from "./ImpersonationBanner";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import styles from "./AppShell.module.css";

const TITLES: Record<string, string> = {
  dashboard: "Dashboard",
  inbox: "Inbox",
  accounts: "Accounts",
  orchestrator: "Orchestrator",
  signals: "Signals",
  alerts: "Alerts",
  members: "Members",
};

function titleFor(pathname: string): string {
  const seg = pathname.split("/").filter(Boolean)[0] ?? "dashboard";
  return TITLES[seg] ?? "InfoJoy GTM";
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
    document.title = `${title} · InfoJoy GTM`;
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
