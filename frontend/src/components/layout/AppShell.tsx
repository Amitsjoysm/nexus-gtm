import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/app/AuthContext";
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
  const title = titleFor(location.pathname);

  // Close the mobile drawer on route change; keep the document title in sync.
  useEffect(() => {
    setDrawerOpen(false);
    document.title = `${title} · InfoJoy GTM`;
  }, [location.pathname, title]);

  return (
    <div className={styles.shell}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <Sidebar open={drawerOpen} onNavigate={() => setDrawerOpen(false)} />

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
  );
}
